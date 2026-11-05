// Code node "Verify signed payload" (run once for all items).
//
// The Webhook node has already checked the token's HS256 signature and expiry against
// the "AIOps webhook signature" credential. This node checks what it cannot: that the
// token was issued for this audience, for exactly this payload (its SHA-256 is a claim),
// and for this notification and incident. A token captured in transit is therefore
// useless with any other body, and it expires within minutes.
const crypto = require('crypto');

const ISSUER = 'aiops-engine';
const AUDIENCE = 'aiops-n8n';
const PAYLOAD_VERSION = 1;
const MAX_PAYLOAD_CHARS = 262144;

const request = $('Incident webhook').first().json;
const claims = request.jwtPayload || {};
const raw = request.body ? request.body.payload : undefined;

function reject(reason) {
  return [{ json: { valid: false, reason } }];
}

if (typeof raw !== 'string' || raw.length === 0) {
  return reject('body.payload must be a non-empty JSON string');
}
if (raw.length > MAX_PAYLOAD_CHARS) {
  return reject('payload is too large');
}
if (claims.iss !== ISSUER || claims.aud !== AUDIENCE) {
  return reject('the token has an unexpected issuer or audience');
}
// The hash is part of the signed token, so a plain comparison is enough: without the
// signing key nobody can obtain a token for a different hash.
const digest = crypto.createHash('sha256').update(raw, 'utf8').digest('hex');
if (digest !== claims.payload_sha256) {
  return reject('the payload does not match the token');
}

let data;
try {
  data = JSON.parse(raw);
} catch (error) {
  return reject('the payload is not valid JSON');
}
if (data.version !== PAYLOAD_VERSION) {
  return reject(`unsupported payload version ${data.version}`);
}
if (!data.notification || data.notification.id !== claims.jti) {
  return reject('the payload is for another notification');
}
if (!data.incident || data.incident.id !== claims.sub) {
  return reject('the payload is for another incident');
}
return [{ json: { valid: true, ...data } }];
