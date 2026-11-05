// Code node "Delivery outcome" (run once for each item).
//
// Turns whatever reached this node into one report for the engine: a Telegram
// response (delivered, with its message_id), a Telegram node's error output after its
// retries (failed, with the reason), or the Switch's "suppress" branch (suppressed).
const MAX_ERROR_CHARS = 500;

const message = $('Format message').item.json;
const input = $json;

function describe(error) {
  if (!error) return 'unknown error';
  if (typeof error === 'string') return error;
  const parts = [error.message, error.description].filter(Boolean);
  return parts.length ? [...new Set(parts)].join(': ') : JSON.stringify(error);
}

let report;
if (message.route === 'suppress') {
  report = { status: 'suppressed' };
} else {
  const sent = input.result && input.result.message_id !== undefined ? input.result : input;
  if (sent && sent.message_id !== undefined && !input.error) {
    report = { status: 'delivered', channel: 'telegram', channel_message_id: String(sent.message_id) };
  } else {
    report = {
      status: 'failed',
      channel: 'telegram',
      error: describe(input.error).slice(0, MAX_ERROR_CHARS),
    };
  }
}

return {
  json: {
    notification_id: message.notification_id,
    report: { ...report, route: message.route, execution_id: String($execution.id) },
  },
};
