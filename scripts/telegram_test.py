#!/usr/bin/env python3
"""Check the Telegram setup: the bot token, the chat, and one test message.

    make telegram-test          # TELEGRAM_CHAT_ID (and the optional settings) from .env
    python3 scripts/telegram_test.py --chat-id 123456789 --no-message

The bot token is read from a file (secrets/telegram_bot_token by default) and never
printed: every error message here is built without the request URL, which contains it.
HTTPS_PROXY and NO_PROXY apply as for any urllib client; `make telegram-test` passes
the proxy n8n uses (N8N_HTTPS_PROXY in .env).

Through a slow or filtered link a Bot API call can take 5-15 s, so every call gets a
long timeout and is retried with exponential back-off (or after the delay a 429 asks
for). The time each call took is printed: n8n's Telegram calls take about as long.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TOKEN = re.compile(r"^\d{5,20}:[A-Za-z0-9_-]{30,}$")
CHAT_ID = re.compile(r"^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$")
DEFAULT_TEXT = (
    "AIOps incident engine: Telegram test message (make telegram-test). "
    "If you can read this, n8n can reach this chat."
)


class TelegramError(Exception):
    pass


class Bot:
    def __init__(self, base_url: str, token: str, timeout: float, attempts: int, backoff: float):
        self._url = f"{base_url.rstrip('/')}/bot{token}"
        self._timeout = timeout
        self._attempts = attempts
        self._backoff = backoff

    def _once(self, method: str, params: dict[str, Any]) -> tuple[Any, float | None, str]:
        """(result, retry_after, error): result on success, else a reason to retry or stop."""
        request = urllib.request.Request(
            f"{self._url}/{method}",
            data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                body = json.load(error)
            except ValueError:
                body = {}
            description = body.get("description") or f"HTTP {error.code}"
            if error.code == 429:  # noqa: PLR2004 - Too Many Requests
                return None, float(body.get("parameters", {}).get("retry_after", 1)), description
            if error.code >= 500:  # noqa: PLR2004
                return None, 0.0, description
            raise TelegramError(description) from None  # 400 chat not found, 401, 403 ...
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            reason = getattr(error, "reason", error)
            return (
                None,
                0.0,
                f"{type(reason).__name__}: {reason}" if str(reason) else type(reason).__name__,
            )
        if not body.get("ok"):
            raise TelegramError(body.get("description", "the answer was not ok"))
        return body["result"], None, ""

    def call(self, method: str, **params: Any) -> tuple[Any, float, int]:
        """The result, the seconds the successful attempt took, and the attempts made."""
        error = ""
        for attempt in range(1, self._attempts + 1):
            started = time.monotonic()
            result, retry_after, error = self._once(method, params)
            if retry_after is None:
                return result, time.monotonic() - started, attempt
            elapsed = time.monotonic() - started
            print(f"  {method}: attempt {attempt} failed after {elapsed:.1f} s: {error}")
            if attempt < self._attempts:
                time.sleep(max(retry_after, self._backoff * 2 ** (attempt - 1)))
        msg = f"gave up after {self._attempts} attempts: {error}"
        raise TelegramError(msg)


class ConfigError(Exception):
    pass


def _settings(token_file: Path, chat_id: str) -> tuple[str, str]:
    """The token and the chat ID, checked; warns about a token of an unusual shape."""
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        msg = f"cannot read {token_file}: {exc.strerror}"
        raise ConfigError(msg) from None
    if not token:
        msg = f"{token_file} is empty: put the token @BotFather gave you in it"
        raise ConfigError(msg)
    if not TOKEN.match(token):
        print(f"warning: the token in {token_file} does not look like a bot token")
    chat_id = chat_id.strip()
    if chat_id and not CHAT_ID.match(chat_id):
        msg = "TELEGRAM_CHAT_ID must be a numeric chat ID (a user, group or channel) or @channel"
        raise ConfigError(msg)
    return token, chat_id


def find_chats(bot: Bot) -> int:
    """Chats from the bot's pending updates: send the bot a message first."""
    updates, _, _ = bot.call("getUpdates", limit=100)
    chats: dict[int, dict[str, Any]] = {}
    for update in updates:
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = update.get(key, {}).get("chat")
            if chat:
                chats[chat["id"]] = chat
    if not chats:
        print("No chats yet: send the bot a message (or add it to the group or channel), retry.")
        return 1
    print("Chats that wrote to the bot recently (put one ID into TELEGRAM_CHAT_ID in .env):")
    for chat_id, chat in chats.items():
        name = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
        print(f"  {chat_id:>16}  {chat.get('type'):<10} {name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path(
            os.environ.get("TELEGRAM_BOT_TOKEN_FILE") or ROOT / "secrets" / "telegram_bot_token"
        ),
    )
    parser.add_argument("--chat-id", default=os.environ.get("TELEGRAM_CHAT_ID", ""))
    parser.add_argument(
        "--base-url", default=os.environ.get("TELEGRAM_API_BASE_URL") or "https://api.telegram.org"
    )
    parser.add_argument("--timeout", type=float, default=45, help="seconds per attempt")
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument(
        "--backoff", type=float, default=2, help="first retry delay, doubled each time"
    )
    parser.add_argument("--no-message", action="store_true", help="check, but send nothing")
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument(
        "--find-chat",
        action="store_true",
        help="list the chats that recently wrote to the bot (or added it), with their IDs",
    )
    args = parser.parse_args()
    try:
        token, chat_id = _settings(args.token_file, args.chat_id)
    except ConfigError as exc:
        print(exc)
        return 2

    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    print(f"Bot API {args.base_url}" + (" through a proxy" if proxy else ""))
    bot = Bot(args.base_url, token, args.timeout, args.attempts, args.backoff)
    total = time.monotonic()
    try:
        me, seconds, attempts = bot.call("getMe")
        print(f"getMe        ok in {seconds:5.1f} s ({attempts} attempt(s)): @{me.get('username')}")
        if args.find_chat:
            return find_chats(bot)
        if not chat_id:
            print("TELEGRAM_CHAT_ID is not set: send the bot a message, then see the README")
            return 2
        chat, seconds, attempts = bot.call("getChat", chat_id=chat_id)
        kind = chat.get("type")
        print(f"getChat      ok in {seconds:5.1f} s ({attempts} attempt(s)): a {kind} chat")
        if not args.no_message:
            sent, seconds, attempts = bot.call("sendMessage", chat_id=chat_id, text=args.text)
            print(
                f"sendMessage  ok in {seconds:5.1f} s ({attempts} attempt(s)): "
                f"message {sent.get('message_id')}"
            )
    except TelegramError as exc:
        print(f"FAILED: {exc}")
        if "chat not found" in str(exc):
            print("  The chat ID is wrong, or the bot has not been started in that chat.")
        return 1
    print(f"Telegram works ({time.monotonic() - total:.1f} s in total).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
