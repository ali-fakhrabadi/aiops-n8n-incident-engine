// One-shot n8n setup: import the AIOps credentials and the routing workflow, then
// publish it. Runs in the n8n image before n8n starts (the "n8n-setup" service).
//
// Credentials are built from Docker secrets and environment variables, written to a
// file on tmpfs only for the n8n CLI to read, and deleted afterwards; n8n stores them
// encrypted with N8N_ENCRYPTION_KEY. Nothing secret is ever printed.
//
// Idempotent: the workflow and credentials have fixed IDs, so a rerun updates them.
// When nothing changed since the last run (a hash kept in n8n's data volume), it does
// nothing at all, so `docker compose up` stays fast.
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join } from 'node:path';

// n8n's own bcrypt implementation, from its installation.
const bcrypt = createRequire('/usr/local/lib/node_modules/n8n/package.json')('bcryptjs');
// n8n reads the owner's password hash from here (N8N_INSTANCE_OWNER_PASSWORD_HASH_FILE).
const OWNER_HASH_FILE = '/home/node/.n8n/aiops-owner-password.bcrypt';

const WORKFLOW_FILE = process.env.AIOPS_WORKFLOW_FILE || '/opt/aiops/workflows/aiops_incident_routing.json';
const STATE_FILE = '/home/node/.n8n/aiops-setup.sha256';
const N8N_URL = 'http://n8n:5678/healthz';
const CHAT_ID = /^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$/;
const BOT_TOKEN = /^\d{5,20}:[A-Za-z0-9_-]{30,}$/;

function log(message) {
  console.log(`[aiops-setup] ${message}`);
}

function secret(name, { required = true } = {}) {
  try {
    return readFileSync(`/run/secrets/${name}`, 'utf8').trim();
  } catch (error) {
    if (required) throw new Error(`secret ${name} is missing (${error.code})`);
    return '';
  }
}

function setting(name, fallback = '') {
  return (process.env[name] || fallback).trim();
}

function telegram() {
  const token = secret('telegram_bot_token', { required: false });
  const chatId = setting('TELEGRAM_CHAT_ID');
  const baseUrl = setting('TELEGRAM_API_BASE_URL', 'https://api.telegram.org').replace(/\/+$/, '');
  if (!token) log('WARNING: secrets/telegram_bot_token is empty: Telegram deliveries will fail.');
  else if (!BOT_TOKEN.test(token)) log('WARNING: the Telegram bot token does not look like one.');
  if (!chatId) log('WARNING: TELEGRAM_CHAT_ID is not set: Telegram deliveries will fail.');
  else if (!CHAT_ID.test(chatId)) throw new Error('TELEGRAM_CHAT_ID must be a numeric chat ID or an @channel name');
  if (!/^https?:\/\//.test(baseUrl)) throw new Error('TELEGRAM_API_BASE_URL must be an http(s) URL');
  return { token, chatId: chatId || 'SET_YOUR_TELEGRAM_CHAT_ID', baseUrl };
}

function credentialData(type, telegramSettings) {
  switch (type) {
    case 'jwtAuth':
      return { keyType: 'passphrase', secret: secret('n8n_webhook_secret'), algorithm: 'HS256' };
    case 'httpHeaderAuth':
      return { name: 'Authorization', value: `Bearer ${secret('callback_token')}` };
    case 'telegramApi':
      return { accessToken: telegramSettings.token, baseUrl: telegramSettings.baseUrl };
    default:
      throw new Error(`the workflow uses a credential type this setup does not know: ${type}`);
  }
}

// The credentials the workflow refers to, by ID: the workflow file is the one place
// where their IDs and names are defined.
function credentials(workflow, telegramSettings) {
  const byId = new Map();
  for (const node of workflow.nodes) {
    for (const [type, ref] of Object.entries(node.credentials || {})) {
      byId.set(ref.id, { id: ref.id, name: ref.name, type, data: credentialData(type, telegramSettings) });
    }
  }
  return [...byId.values()];
}

function configure(workflow, telegramSettings) {
  const settings = workflow.nodes.find((node) => node.name === 'Settings');
  if (!settings) throw new Error('the workflow has no "Settings" node');
  const values = {
    telegram_chat_id: telegramSettings.chatId,
    aiops_api_url: setting('AIOPS_API_URL', 'http://api:8000').replace(/\/+$/, ''),
  };
  for (const assignment of settings.parameters.assignments.assignments) {
    if (assignment.name in values) assignment.value = values[assignment.name];
  }
  return workflow;
}

function n8n(...args) {
  execFileSync('n8n', args, { stdio: ['ignore', 'inherit', 'inherit'] });
}

async function n8nIsRunning() {
  try {
    const response = await fetch(N8N_URL, { signal: AbortSignal.timeout(2000) });
    return response.ok;
  } catch {
    return false;
  }
}

// The owner account is managed from the environment (N8N_INSTANCE_OWNER_MANAGED_BY_ENV), so
// the editor never shows a setup page anyone could claim first. Its password is a secret;
// n8n wants a bcrypt hash of it, rewritten only when the password changes.
function ownerHash() {
  const password = secret('n8n_owner_password');
  try {
    if (bcrypt.compareSync(password, readFileSync(OWNER_HASH_FILE, 'utf8').trim())) return;
  } catch {}
  writeFileSync(OWNER_HASH_FILE, `${bcrypt.hashSync(password, 12)}\n`, { mode: 0o600 });
  log('owner password hash written');
}

async function main() {
  ownerHash();
  const telegramSettings = telegram();
  const workflow = configure(JSON.parse(readFileSync(WORKFLOW_FILE, 'utf8')), telegramSettings);
  const creds = credentials(workflow, telegramSettings);
  const digest = createHash('sha256').update(JSON.stringify({ workflow, creds })).digest('hex');
  let previous = '';
  try {
    previous = readFileSync(STATE_FILE, 'utf8').trim();
  } catch {}
  if (previous === digest && !process.argv.includes('--force')) {
    log('workflow and credentials are up to date');
    return;
  }

  const dir = mkdtempSync('/tmp/aiops-setup-');
  try {
    const credentialsFile = join(dir, 'credentials.json');
    const workflowFile = join(dir, 'workflow.json');
    writeFileSync(credentialsFile, JSON.stringify(creds), { mode: 0o600 });
    writeFileSync(workflowFile, JSON.stringify([workflow]), { mode: 0o600 });
    n8n('import:credentials', `--input=${credentialsFile}`);
    n8n('import:workflow', `--input=${workflowFile}`);
    n8n('publish:workflow', `--id=${workflow.id}`);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
  writeFileSync(STATE_FILE, `${digest}\n`, { mode: 0o600 });
  log(`imported ${creds.length} credentials and published workflow "${workflow.name}"`);
  if (await n8nIsRunning()) {
    log('n8n is running and loads published workflows at start: run `make n8n-setup` to restart it');
  }
}

main().catch((error) => {
  console.error(`[aiops-setup] ${error.message}`);
  process.exit(1);
});
