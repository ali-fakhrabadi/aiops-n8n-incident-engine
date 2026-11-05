#!/usr/bin/env python3
"""A fake Telegram Bot API for tests and demos. Standard library only.

Answers getMe, getChat and sendMessage like api.telegram.org, checks the bot token
against a file, and checks sendMessage's HTML the way Telegram does (only Telegram's
tags, properly nested, no bare <, > or &), so a formatting mistake fails here as it
would there. Faults can be injected to exercise n8n's retries:

    POST /_test/faults {"status": 502, "count": 2}  the next 2 sendMessage calls fail
    POST /_test/faults {"delay": 8}                  every Bot API call waits 8 s
    GET  /_test/messages                             what was sent, oldest first
    POST /_test/reset                                forget messages and faults

    python3 scripts/fake_telegram_api.py --port 8081 --token-file secrets/telegram_bot_token
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

MAX_TEXT = 4096
# https://core.telegram.org/bots/api#html-style
TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "a", "code", "pre", "tg-spoiler", "span", "blockquote", "tg-emoji",
}  # fmt: skip
BOT_PATH = re.compile(r"^/bot(?P<token>[^/]+)/(?P<method>[A-Za-z]+)$")


class EntityError(ValueError):
    pass


class TelegramHtml(HTMLParser):
    """Parses like Telegram's HTML mode; `length` is the text length without markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.open: list[str] = []
        self.length = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in TAGS:
            msg = f'Unsupported start tag "{tag}"'
            raise EntityError(msg)
        self.open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.open or self.open.pop() != tag:
            msg = f'Unexpected end tag "{tag}"'
            raise EntityError(msg)

    def handle_data(self, data: str) -> None:
        if any(char in data for char in "<>&"):
            msg = "Bare <, > or & in text: escape it as &lt;, &gt; or &amp;"
            raise EntityError(msg)
        self.length += len(data)

    def handle_entityref(self, name: str) -> None:
        if name not in {"lt", "gt", "amp", "quot"}:
            msg = f'Unsupported HTML entity "&{name};"'
            raise EntityError(msg)
        self.length += 1

    def handle_charref(self, name: str) -> None:
        self.length += 1

    def check(self, text: str) -> int:
        self.feed(text)
        self.close()
        if self.open:
            msg = f'Can\'t find end tag corresponding to start tag "{self.open[-1]}"'
            raise EntityError(msg)
        return self.length


@dataclass
class State:
    token: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    messages: list[dict[str, Any]] = field(default_factory=list)
    fail_status: int = 0
    fail_count: int = 0
    delay: float = 0.0

    def reset(self) -> None:
        with self.lock:
            self.messages.clear()
            self.fail_status = self.fail_count = 0
            self.delay = 0.0


def _error(code: int, description: str, **extra: Any) -> tuple[int, dict[str, Any]]:
    return code, {"ok": False, "error_code": code, "description": description, **extra}


def _chat(params: dict[str, Any]) -> int | str | None:
    chat_id = params.get("chat_id")
    if chat_id in (None, ""):
        return None
    text = str(chat_id)
    return int(text) if re.fullmatch(r"-?\d+", text) else text


def _text_problem(text: str, parse_mode: object) -> str | None:
    """Why Telegram would refuse this text, or None."""
    if not text.strip():
        return "message text is empty"
    length = len(text)
    if parse_mode == "HTML":
        try:
            length = TelegramHtml().check(text)
        except EntityError as exc:
            return f"can't parse entities: {exc}"
    return "message is too long" if length > MAX_TEXT else None


def send_message(state: State, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    chat_id = _chat(params)
    if chat_id is None:
        return _error(400, "Bad Request: chat_id is empty")
    text = str(params.get("text") or "")
    if problem := _text_problem(text, params.get("parse_mode")):
        return _error(400, f"Bad Request: {problem}")
    with state.lock:
        if state.fail_count > 0:
            state.fail_count -= 1
            if state.fail_status == HTTPStatus.TOO_MANY_REQUESTS:
                return _error(
                    429, "Too Many Requests: retry after 1", parameters={"retry_after": 1}
                )
            return _error(state.fail_status, "Bad Gateway")
        reply_to = params.get("reply_to_message_id")
        if reply_to not in (None, "") and not any(
            str(sent["message_id"]) == str(reply_to) and sent["chat_id"] == chat_id
            for sent in state.messages
        ):
            return _error(400, "Bad Request: message to be replied not found")
        message_id = len(state.messages) + 1
        state.messages.append(
            {
                "message_id": message_id,
                "chat_id": chat_id,
                "text": text,
                "parse_mode": params.get("parse_mode"),
                "disable_notification": bool(params.get("disable_notification")),
                "reply_to_message_id": reply_to,
                "received_at": time.time(),
            }
        )
    return 200, {
        "ok": True,
        "result": {
            "message_id": message_id,
            "date": int(time.time()),
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }


def bot_method(state: State, method: str, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    if method == "getMe":
        bot = {"id": 100000001, "is_bot": True, "first_name": "AIOps test bot"}
        return 200, {"ok": True, "result": {**bot, "username": "aiops_test_bot"}}
    if method == "getChat":
        chat_id = _chat(params)
        if chat_id is None:
            return _error(400, "Bad Request: chat_id is empty")
        return 200, {"ok": True, "result": {"id": chat_id, "type": "private"}}
    if method == "sendMessage":
        return send_message(state, params)
    return _error(404, "Not Found")


class Handler(BaseHTTPRequestHandler):
    server_version = "FakeTelegramBotAPI/1"
    state: State

    def log_message(self, format: str, *args: Any) -> None:
        # The token is part of the path: never log it.
        line = re.sub(r"/bot[^/\s]+/", "/bot<token>/", format % args)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    def _reply(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _params(self) -> dict[str, Any]:
        url = urlsplit(self.path)
        params: dict[str, Any] = dict(parse_qsl(url.query))
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            raw = self.rfile.read(length)
            content_type = self.headers.get("Content-Type", "")
            if "json" in content_type:
                params.update(json.loads(raw))
            else:
                params.update(parse_qsl(raw.decode()))
        return params

    def _test(self, path: str) -> None:
        state = self.state
        if path == "/_test/health":
            self._reply(200, {"ok": True})
        elif path == "/_test/messages":
            with state.lock:
                self._reply(200, {"messages": list(state.messages)})
        elif path == "/_test/reset" and self.command == "POST":
            state.reset()
            self._reply(200, {"ok": True})
        elif path == "/_test/faults" and self.command == "POST":
            faults = self._params()
            with state.lock:
                state.fail_status = int(faults.get("status", 502))
                state.fail_count = int(faults.get("count", 0))
                state.delay = float(faults.get("delay", 0))
            self._reply(200, {"ok": True})
        else:
            self._reply(404, {"ok": False})

    def _handle(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith("/_test/"):
            self._test(path)
            return
        match = BOT_PATH.match(path)
        if match is None:
            self._reply(*_error(404, "Not Found"))
            return
        if match["token"] != self.state.token:
            self._reply(*_error(401, "Unauthorized"))
            return
        try:
            params = self._params()
        except (ValueError, UnicodeDecodeError):
            self._reply(*_error(400, "Bad Request: invalid request body"))
            return
        if self.state.delay:
            time.sleep(self.state.delay)
        self._reply(*bot_method(self.state, match["method"], params))

    do_GET = _handle  # noqa: N815 - BaseHTTPRequestHandler's names
    do_POST = _handle  # noqa: N815


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - a container's only service
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path(os.environ.get("FAKE_TELEGRAM_TOKEN_FILE", "secrets/telegram_bot_token")),
    )
    args = parser.parse_args()
    token = args.token_file.read_text(encoding="utf-8").strip()
    if not token:
        print(f"{args.token_file} is empty: the fake API needs the token n8n will use")
        return 2
    Handler.state = State(token=token)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"fake Telegram Bot API on {args.host}:{args.port}", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
