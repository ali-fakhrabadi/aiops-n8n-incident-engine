// Unit tests of the workflow's Code nodes: `make test-workflow` (node --test).
//
// Each script runs as n8n runs it: the body of an async function with n8n's helpers in
// scope ($, $json, $execution, require). `nodes` holds the output of earlier nodes.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const CODE = join(dirname(fileURLToPath(import.meta.url)), '..', 'code');
const AsyncFunction = Object.getPrototypeOf(async () => {}).constructor;
const nodeRequire = createRequire(import.meta.url);

async function run(file, { nodes = {}, json = {}, executionId = 7 } = {}) {
  const $ = (name) => {
    if (!(name in nodes)) throw new Error(`no output of node "${name}"`);
    const item = { json: nodes[name] };
    return { first: () => item, item };
  };
  const script = readFileSync(join(CODE, file), 'utf8');
  return new AsyncFunction('$', '$json', '$execution', 'require', script)(
    $,
    json,
    { id: executionId },
    nodeRequire,
  );
}

const sha256 = (text) => createHash('sha256').update(text, 'utf8').digest('hex');

function payload(overrides = {}) {
  return {
    version: 1,
    notification: { id: 'n-1', kind: 'firing', created_at: '2026-11-05T09:01:30+00:00' },
    incident: {
      id: 'i-1',
      alertname: 'ContainerRestarting',
      severity: 'critical',
      status: 'firing',
      flapping: false,
      starts_at: '2026-11-05T09:00:00+00:00',
      resolved_at: null,
      first_received_at: '2026-11-05T09:00:01+00:00',
      labels: { compose_project: 'shop', compose_service: 'api', severity: 'critical' },
      annotations: { summary: 'Docker restarted shop-api-1 4 times in 15 minutes.' },
      generator_url: null,
    },
    analysis: {
      status: 'completed',
      provider: 'ollama',
      model: 'llama3.1',
      confidence: 'medium',
      summary: 'The API runs out of memory.',
      probable_cause: 'An export loads 1.8 million rows into memory and the worker is killed.',
      evidence: [{ quote: 'MemoryError: Cannot allocate memory', why: 'the crash' }],
      suggested_commands: [{ command: 'docker stats --no-stream shop-api-1', purpose: 'memory' }],
      next_steps: ['Stream the export instead of loading it at once'],
      commands_rejected: 0,
    },
    ...overrides,
  };
}

// What the Webhook node outputs for a request signed like the worker signs it.
function request(data, claims = {}) {
  const raw = JSON.stringify(data);
  return {
    body: { payload: raw },
    jwtPayload: {
      iss: 'aiops-engine',
      aud: 'aiops-n8n',
      sub: data.incident.id,
      jti: data.notification.id,
      payload_sha256: sha256(raw),
      ...claims,
    },
  };
}

async function verify(webhook) {
  const [item] = await run('verify_payload.js', { nodes: { 'Incident webhook': webhook } });
  return item.json;
}

test('a payload the token was issued for is accepted', async () => {
  const result = await verify(request(payload()));

  assert.equal(result.valid, true);
  assert.equal(result.incident.alertname, 'ContainerRestarting');
  assert.equal(result.analysis.provider, 'ollama');
});

test('a payload changed after signing is rejected', async () => {
  const webhook = request(payload());
  webhook.body.payload = webhook.body.payload.replace('critical', 'info');

  assert.deepEqual(await verify(webhook), {
    valid: false,
    reason: 'the payload does not match the token',
  });
});

test('a token for another notification, incident or audience is rejected', async () => {
  const cases = [
    [{ jti: 'n-2' }, 'the payload is for another notification'],
    [{ sub: 'i-2' }, 'the payload is for another incident'],
    [{ aud: 'someone-else' }, 'the token has an unexpected issuer or audience'],
    [{ iss: 'someone-else' }, 'the token has an unexpected issuer or audience'],
  ];
  for (const [claims, reason] of cases) {
    assert.deepEqual(await verify(request(payload(), claims)), { valid: false, reason });
  }
});

test('malformed bodies are rejected', async () => {
  const signed = request(payload());
  const notJson = { ...signed, body: { payload: '{not json' } };
  notJson.jwtPayload = { ...signed.jwtPayload, payload_sha256: sha256('{not json') };
  const v2 = request(payload({ version: 2 }));

  assert.equal((await verify({ ...signed, body: { payload: { version: 1 } } })).valid, false);
  assert.equal((await verify({ ...signed, body: undefined })).valid, false);
  assert.equal((await verify(notJson)).reason, 'the payload is not valid JSON');
  assert.equal((await verify(v2)).reason, 'unsupported payload version 2');
});

async function format(data) {
  const verified = { valid: true, ...data };
  const [item] = await run('format_message.js', { nodes: { 'Verify signed payload': verified } });
  return item.json;
}

function incident(overrides) {
  return payload({ incident: { ...payload().incident, ...overrides } });
}

test('routing: critical and firing pages, other critical and warning are quiet, the rest is recorded', async () => {
  const cases = [
    [{ severity: 'critical' }, 'page'],
    [{ severity: 'critical', flapping: true }, 'quiet'],
    [{ severity: 'critical', status: 'resolved', resolved_at: '2026-11-05T09:04:00+00:00' }, 'quiet'],
    [{ severity: 'warning' }, 'quiet'],
    [{ severity: 'info' }, 'suppress'],
    [{ severity: 'none' }, 'suppress'],
    [{ severity: 'unknown' }, 'suppress'],
  ];
  for (const [overrides, route] of cases) {
    const message = await format(incident(overrides));
    assert.equal(message.route, route, JSON.stringify(overrides));
    assert.equal(message.disable_notification, route !== 'page');
  }
});

// Telegram's HTML mode: only its tags, properly nested, and no bare <, > or &.
const TAGS = new Set(['b', 'i', 'u', 's', 'code', 'pre', 'a', 'blockquote', 'tg-spoiler']);
function assertTelegramHtml(text) {
  const open = [];
  for (const match of text.matchAll(/<(\/?)([a-z-]+)[^<>]*>/g)) {
    const [, closing, tag] = match;
    assert.ok(TAGS.has(tag), `tag <${tag}> is not one Telegram accepts`);
    if (closing) assert.equal(open.pop(), tag, `</${tag}> closes the wrong tag`);
    else open.push(tag);
  }
  assert.deepEqual(open, [], 'unclosed tags');
  const plain = text.replace(/<\/?[a-z-]+[^<>]*>/g, '').replace(/&(lt|gt|amp|quot);/g, '');
  assert.doesNotMatch(plain, /[<>&]/, 'bare <, > or & in the text');
}

test('the message carries the alert and the checked analysis', async () => {
  const { text } = await format(payload());

  assertTelegramHtml(text);
  assert.match(text, /🔴 <b>CRITICAL<\/b> · <b>ContainerRestarting<\/b>/);
  assert.match(text, /compose_project=<code>shop<\/code>/);
  assert.match(text, /Started 2026-11-05 09:00 UTC/);
  assert.match(text, /<b>Probable cause<\/b> \(medium confidence, ollama llama3\.1\)/);
  assert.match(text, /<pre>docker stats --no-stream shop-api-1<\/pre>/);
  assert.match(text, /• <code>MemoryError: Cannot allocate memory<\/code>/);
  assert.match(text, /1\. Stream the export/);
  assert.match(text, /Incident <code>i-1<\/code>$/);
});

test('everything from the payload is escaped', async () => {
  const hostile = payload({
    incident: {
      ...payload().incident,
      alertname: '<b>Fake</b> & <a href="https://evil.example">click</a>',
      labels: { compose_service: '</code><i>x' },
      annotations: { summary: 'ignore <previous> instructions' },
    },
    analysis: {
      ...payload().analysis,
      probable_cause: '</pre><script>alert(1)</script>',
      evidence: [{ quote: '</code>&amp;', why: '' }],
      suggested_commands: [{ command: 'cat /var/log/app.log && echo </pre>', purpose: '' }],
      next_steps: ['<tg-spoiler>hidden</tg-spoiler>'],
    },
  });

  const { text } = await format(hostile);

  assertTelegramHtml(text);
  assert.doesNotMatch(text, /<script|<a |<previous>|<tg-spoiler>/);
  assert.match(text, /&lt;b&gt;Fake&lt;\/b&gt; &amp; &lt;a href="https:\/\/evil\.example"&gt;/);
  assert.match(text, /&amp;amp;/);
});

test('long analyses are cut to fit one Telegram message', async () => {
  const long = 'x'.repeat(5000);
  const big = payload({
    analysis: {
      ...payload().analysis,
      probable_cause: long,
      evidence: Array.from({ length: 5 }, () => ({ quote: long, why: long })),
      suggested_commands: Array.from({ length: 6 }, () => ({ command: `df -h ${long}`, purpose: '' })),
      next_steps: Array.from({ length: 6 }, () => long),
    },
  });

  const { text } = await format(big);

  assert.ok(text.length <= 4096, `${text.length} characters`);
  assertTelegramHtml(text);
  assert.match(text, /^🔴 <b>CRITICAL<\/b>/);
  assert.match(text, /Incident <code>i-1<\/code>$/);
});

test('the all-clear is short and says how long it lasted', async () => {
  const resolved = payload({
    notification: { id: 'n-2', kind: 'resolved' },
    incident: { ...payload().incident, status: 'resolved', resolved_at: '2026-11-05T09:12:00+00:00' },
  });

  const { text, route } = await format(resolved);

  assert.equal(route, 'quiet');
  assert.match(text, /^✅ <b>RESOLVED<\/b> · <b>ContainerRestarting<\/b>/);
  assert.match(text, /resolved after 12 min/);
  assert.doesNotMatch(text, /Probable cause/);
});

test('a failed analysis says why, and withheld commands are counted', async () => {
  const failed = await format(
    payload({ analysis: { status: 'failed', provider: 'ollama', reason: 'model <timeout>' } }),
  );
  const withheld = await format(payload({ analysis: { ...payload().analysis, commands_rejected: 2 } }));

  assert.match(failed.text, /<i>No analysis: model &lt;timeout&gt;<\/i>/);
  assert.match(withheld.text, /2 suggested command\(s\) withheld: not read-only/);
});

test('a page goes out with sound and without an analysis', async () => {
  const page = payload({
    notification: { id: 'n-0', kind: 'page' },
    analysis: { status: 'pending' },
  });

  const message = await format(page);

  assert.equal(message.route, 'page');
  assert.equal(message.disable_notification, false);
  assert.equal(message.reply_to_message_id, null);
  assertTelegramHtml(message.text);
  assert.match(message.text, /^🔴 <b>CRITICAL<\/b> · <b>ContainerRestarting<\/b>/);
  assert.match(message.text, /<i>Analysis follows\.<\/i>/);
  assert.doesNotMatch(message.text, /Probable cause/);
});

test('the analysis after a page is a silent reply to it', async () => {
  const followUp = payload({
    notification: { id: 'n-1', kind: 'firing', follows_page: true, reply_to_message_id: '42' },
  });

  const message = await format(followUp);

  assert.equal(message.route, 'reply');
  assert.equal(message.disable_notification, true);
  assert.equal(message.reply_to_message_id, 42);
  assertTelegramHtml(message.text);
  assert.match(message.text, /^🔎 <b>Analysis<\/b> · <b>ContainerRestarting<\/b>\nStill firing since/);
  assert.match(message.text, /<b>Probable cause<\/b>/);
  assert.doesNotMatch(message.text, /compose_project=/); // the page already said where
});

test('without the page message ID the analysis still goes out, silently', async () => {
  const followUp = payload({
    notification: { id: 'n-1', kind: 'firing', follows_page: true, reply_to_message_id: null },
  });

  const message = await format(followUp);

  assert.equal(message.route, 'quiet');
  assert.equal(message.reply_to_message_id, null);
});

async function outcome(route, json) {
  const nodes = { 'Format message': { route, notification_id: 'n-1' } };
  return (await run('delivery_outcome.js', { nodes, json })).json;
}

test('a Telegram answer is reported as delivered', async () => {
  const report = await outcome('page', { ok: true, result: { message_id: 42, chat: { id: 1 } } });

  assert.deepEqual(report, {
    notification_id: 'n-1',
    report: {
      status: 'delivered',
      channel: 'telegram',
      channel_message_id: '42',
      route: 'page',
      execution_id: '7',
    },
  });
});

test('a Telegram error after the retries is reported as failed, with the reason', async () => {
  const error = {
    message: 'Bad request - please check your parameters',
    description: 'Bad Request: chat not found',
  };

  const report = await outcome('quiet', { error });

  assert.equal(report.report.status, 'failed');
  assert.equal(
    report.report.error,
    'Bad request - please check your parameters: Bad Request: chat not found',
  );
  assert.equal((await outcome('quiet', { error: 'ETIMEDOUT' })).report.error, 'ETIMEDOUT');
});

test('the suppress branch is reported as suppressed', async () => {
  const report = await outcome('suppress', { route: 'suppress', text: '...' });

  assert.deepEqual(report.report, { status: 'suppressed', route: 'suppress', execution_id: '7' });
});
