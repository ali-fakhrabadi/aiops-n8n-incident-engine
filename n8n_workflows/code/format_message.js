// Code node "Format message" (run once for all items).
//
// Picks the route and builds the Telegram message (HTML parse mode). Every value from
// the payload is escaped: alert labels, log quotes and model output can contain
// anything, including markup meant to fake a message or hide a link.
//
// Routes (the Switch node after this one sends each to its own branch):
//   page      critical and firing, not flapping: a message with sound
//   quiet     any other critical or warning (flapping, resolved): a silent message
//   suppress  info and everything else: recorded, not sent
const MAX_LENGTH = 4096; // Telegram's limit for one message
const LABELS = ['compose_project', 'compose_service', 'container', 'service', 'instance', 'job'];
const ICONS = { critical: '🔴', warning: '🟠', info: '🔵' };

const { notification, incident, analysis } = $('Verify signed payload').first().json;

function escape(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

// Shorten before escaping, so an entity is never cut in half.
function clip(value, max) {
  const text = String(value ?? '').trim();
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function route() {
  const severity = incident.severity;
  if (severity === 'critical' && incident.status === 'firing' && !incident.flapping) {
    return 'page';
  }
  if (severity === 'critical' || severity === 'warning') {
    return 'quiet';
  }
  return 'suppress';
}

function clock(iso) {
  return iso ? `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC` : 'unknown';
}

function duration(fromIso, toIso) {
  const seconds = Math.max(0, Math.round((Date.parse(toIso) - Date.parse(fromIso)) / 1000));
  if (Number.isNaN(seconds)) return 'unknown';
  if (seconds < 120) return `${seconds} s`;
  if (seconds < 7200) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

function header() {
  const resolved = incident.status === 'resolved';
  const icon = resolved ? '✅' : ICONS[incident.severity] || '⚪';
  const state = resolved ? 'RESOLVED' : String(incident.severity || 'unknown').toUpperCase();
  const lines = [`${icon} <b>${escape(state)}</b> · <b>${escape(clip(incident.alertname, 120))}</b>`];
  const annotations = incident.annotations || {};
  if (annotations.summary) lines.push(`<i>${escape(clip(annotations.summary, 300))}</i>`);
  const labels = incident.labels || {};
  const where = LABELS.filter((key) => labels[key])
    .slice(0, 4)
    .map((key) => `${escape(key)}=<code>${escape(clip(labels[key], 80))}</code>`);
  if (where.length) lines.push(where.join(' · '));
  let when = `Started ${clock(incident.starts_at)}`;
  if (resolved && incident.resolved_at) {
    when += `, resolved after ${duration(incident.starts_at, incident.resolved_at)}`;
  }
  lines.push(when);
  if (incident.flapping) lines.push('⚠️ Flapping: this alert keeps firing and resolving.');
  return lines.join('\n');
}

// Sections in order of importance: the last ones are dropped first when too long.
function analysisSections() {
  if (!analysis || analysis.status === 'skipped' || analysis.status === 'pending') return [];
  if (analysis.status === 'failed') {
    return [`<i>No analysis: ${escape(clip(analysis.reason || 'unknown reason', 300))}</i>`];
  }
  const source = [analysis.provider, analysis.model].filter(Boolean).join(' ');
  const sections = [
    `<b>Probable cause</b> (${escape(analysis.confidence || 'unknown')} confidence, ` +
      `${escape(source)})\n${escape(clip(analysis.probable_cause || analysis.summary, 1200))}`,
  ];
  const commands = (analysis.suggested_commands || []).slice(0, 6);
  if (commands.length) {
    const block = commands.map((item) => escape(clip(item.command, 200))).join('\n');
    sections.push(`<b>Read-only checks</b>\n<pre>${block}</pre>`);
  }
  const evidence = (analysis.evidence || []).slice(0, 3);
  if (evidence.length) {
    const quotes = evidence.map((item) => `• <code>${escape(clip(item.quote, 200))}</code>`);
    sections.push(`<b>Evidence</b>\n${quotes.join('\n')}`);
  }
  const steps = (analysis.next_steps || []).slice(0, 4);
  if (steps.length) {
    const list = steps.map((step, index) => `${index + 1}. ${escape(clip(step, 200))}`);
    sections.push(`<b>Next steps</b>\n${list.join('\n')}`);
  }
  if (analysis.commands_rejected > 0) {
    sections.push(`<i>${analysis.commands_rejected} suggested command(s) withheld: not read-only.</i>`);
  }
  return sections;
}

function message() {
  const footer = `Incident <code>${escape(incident.id)}</code>`;
  // The all-clear is short; the firing message carries the analysis.
  const sections = notification.kind === 'resolved' ? [] : analysisSections();
  // Header and footer are clipped field by field and stay far below the limit.
  let text = [header(), ...sections, footer].join('\n\n');
  while (text.length > MAX_LENGTH && sections.length) {
    sections.pop();
    text = [header(), ...sections, footer].join('\n\n');
  }
  return text;
}

const chosen = route();
return [
  {
    json: {
      route: chosen,
      disable_notification: chosen !== 'page',
      text: message(),
      notification_id: notification.id,
      incident_id: incident.id,
      kind: notification.kind,
    },
  },
];
