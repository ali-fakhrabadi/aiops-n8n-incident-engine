from __future__ import annotations

import json
import logging
import sys

from aiops.context import request_id_var
from aiops.logs import REDACTED, JsonFormatter, redact


def _record(message: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("aiops.test", logging.INFO, __file__, 1, message, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_redact_masks_sensitive_keys_at_any_depth() -> None:
    value = {
        "alert": "HighCPU",
        "Authorization": "Bearer abc",
        "nested": {"db_password": "x", "items": [{"api_key": "k", "host": "web-1"}]},
        "token_count": 3,
    }

    assert redact(value) == {
        "alert": "HighCPU",
        "Authorization": REDACTED,
        "nested": {"db_password": REDACTED, "items": [{"api_key": REDACTED, "host": "web-1"}]},
        "token_count": REDACTED,  # cautious by design: a name match is enough
    }


def test_formatter_writes_one_json_object_with_request_id_and_extras() -> None:
    token = request_id_var.set("req-12345678")
    try:
        line = JsonFormatter().format(_record("alert received", alertname="HighCPU", secret="s"))
    finally:
        request_id_var.reset(token)

    payload = json.loads(line)
    assert payload["message"] == "alert received"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "aiops.test"
    assert payload["request_id"] == "req-12345678"
    assert payload["alertname"] == "HighCPU"
    assert payload["secret"] == REDACTED
    assert payload["timestamp"].endswith("+00:00")


def test_formatter_includes_the_traceback() -> None:
    try:
        msg = "boom"
        raise ValueError(msg)
    except ValueError:
        record = _record("failed")
        record.exc_info = sys.exc_info()

    payload = json.loads(JsonFormatter().format(record))

    assert "ValueError: boom" in payload["exception"]
    assert "request_id" not in payload
