"""Structured JSON logging with redaction of sensitive fields.

Every record is one JSON object on stdout, carrying the request ID of the request that
produced it. Values of keys that look like credentials are replaced before they are
written, at any nesting depth, so a careless `extra={...}` cannot leak a token.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from aiops.context import request_id_var

REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"pass(word|wd)?|secret|token|authorization|api[_-]?key|cookie|credential", re.IGNORECASE
)
# Attributes every LogRecord has; anything else on a record came from `extra`.
_RECORD_ATTRIBUTES = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
}


def redact(value: Any, key: str | None = None) -> Any:
    """Return `value` with the values of sensitive-looking keys replaced."""
    if key is not None and _SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, Mapping):
        return {item_key: redact(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id is not None:
            payload["request_id"] = request_id
        for key, value in vars(record).items():
            if key not in _RECORD_ATTRIBUTES and not key.startswith("_"):
                payload[key] = redact(value, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


# Name of the handler installed here, so reconfiguring replaces only that handler.
_HANDLER_NAME = "aiops-json-stdout"


def configure_logging(level: str) -> None:
    """Send all logs, Uvicorn's included, to stdout as JSON."""
    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.get_name() == _HANDLER_NAME:
            root.removeHandler(existing)
    handler = logging.StreamHandler(sys.stdout)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # The request middleware writes one structured line per request instead.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
