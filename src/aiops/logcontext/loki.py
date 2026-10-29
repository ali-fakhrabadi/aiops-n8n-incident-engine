"""Minimal Loki query client (query_range over HTTP)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any

import httpx

# Stream labels worth keeping next to each line; the rest is noise for a reader.
KEPT_LABELS = ("container", "compose_service", "level", "stream")


class LokiUnavailableError(Exception):
    """Loki could not answer now (network, timeout, 5xx, 429): worth retrying."""


class LokiQueryError(Exception):
    """Loki rejected the query (4xx) or answered garbage: retrying will not help."""


@dataclass(frozen=True)
class LogLine:
    # Loki's own nanosecond timestamp: a float cannot hold today's epoch in nanoseconds,
    # so ordering uses this integer, never the datetime.
    nanoseconds: int
    labels: dict[str, str]
    line: str

    @property
    def timestamp(self) -> datetime:
        seconds, rest = divmod(self.nanoseconds, 1_000_000_000)
        return datetime.fromtimestamp(seconds, tz=UTC) + timedelta(microseconds=rest // 1000)


def _nanoseconds(moment: datetime) -> str:
    whole = int(moment.timestamp())
    return str(whole * 1_000_000_000 + moment.microsecond * 1000)


class LokiClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient) -> None:
        self._url = f"{base_url.rstrip('/')}/loki/api/v1/query_range"
        self._http = http

    async def query_range(
        self, query: str, start: datetime, end: datetime, limit: int
    ) -> list[LogLine]:
        """The `limit` newest lines in [start, end], returned oldest first."""
        params = {
            "query": query,
            "start": _nanoseconds(start),
            "end": _nanoseconds(end),
            "limit": str(limit),
            "direction": "backward",
        }
        try:
            response = await self._http.get(self._url, params=params)
        except httpx.HTTPError as exc:
            msg = f"cannot reach Loki: {type(exc).__name__}"
            raise LokiUnavailableError(msg) from exc
        if response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR or response.status_code in (
            HTTPStatus.TOO_MANY_REQUESTS,
            HTTPStatus.REQUEST_TIMEOUT,
        ):
            msg = f"Loki answered HTTP {response.status_code}"
            raise LokiUnavailableError(msg)
        if response.status_code != HTTPStatus.OK:
            msg = f"Loki rejected the query: HTTP {response.status_code}"
            raise LokiQueryError(msg)
        try:
            return _parse(response.json(), limit)
        except (ValueError, KeyError, TypeError) as exc:
            msg = "Loki's answer is not a streams result"
            raise LokiQueryError(msg) from exc


def _parse(body: Any, limit: int) -> list[LogLine]:
    if body["data"]["resultType"] != "streams":
        raise ValueError(body["data"]["resultType"])
    lines: list[LogLine] = []
    for stream in body["data"]["result"]:
        labels = {key: str(value) for key, value in stream["stream"].items() if key in KEPT_LABELS}
        for timestamp, line in stream["values"]:
            lines.append(LogLine(nanoseconds=int(timestamp), labels=labels, line=str(line)))
    lines.sort(key=lambda item: item.nanoseconds)
    return lines[-limit:]
