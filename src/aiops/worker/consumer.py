"""Redis Streams consumer group with retries and a dead-letter stream.

* New messages are read with XREADGROUP; a handled message is acknowledged (XACK).
* A message whose handler failed stays pending. Once it has been idle for
  `claim_idle_seconds`, XAUTOCLAIM hands it to a live consumer again; this is the retry,
  and it also rescues messages of a consumer that crashed.
* After `max_deliveries` attempts the message goes to the dead-letter stream, the
  handler records the failure, and the message is acknowledged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from redis.typing import EncodableT, FieldT

from aiops.config import WorkerSettings
from aiops.worker.handlers import Handler, PermanentError
from aiops.worker.metrics import WorkerMetrics

logger = logging.getLogger(__name__)

READ_COUNT = 10
MAX_BACKOFF_SECONDS = 30.0

# Shapes of redis-py's replies with decode_responses=True (its own annotations are wider).
Entry = tuple[str, dict[str, str]]
StreamsReply = list[tuple[str, list[Entry]]]


class StreamConsumer:
    def __init__(
        self,
        redis: Redis,
        settings: WorkerSettings,
        handlers: Mapping[str, Handler],
        metrics: WorkerMetrics,
        consumer_name: str,
    ) -> None:
        self._redis = redis
        self._settings = settings
        self._handlers = handlers
        self._metrics = metrics
        self._name = consumer_name

    async def ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(
                self._settings.stream_name, self._settings.consumer_group, id="0", mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def run_once(self) -> int:
        """Retry due messages, then read new ones (blocking up to read_block_ms)."""
        handled = await self._retry_due()
        response = await self._redis.xreadgroup(
            self._settings.consumer_group,
            self._name,
            {self._settings.stream_name: ">"},
            count=READ_COUNT,
            block=self._settings.read_block_ms,
        )
        for _stream, entries in cast("StreamsReply | None", response) or []:
            for message_id, fields in entries:
                await self._process(message_id, fields)
                handled += 1
        return handled

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        group_ready = False
        while not stop.is_set():
            try:
                if not group_ready:
                    await self.ensure_group()
                    group_ready = True
                await self.run_once()
                _touch(self._settings.heartbeat_file)
                backoff = 1.0
            except Exception:
                logger.exception(
                    "stream consumer failed; retrying", extra={"backoff_seconds": backoff}
                )
                group_ready = False
                with contextlib.suppress(TimeoutError):  # wake up at once on shutdown
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    async def _retry_due(self) -> int:
        settings = self._settings
        reply = await self._redis.xautoclaim(
            settings.stream_name,
            settings.consumer_group,
            self._name,
            min_idle_time=int(settings.claim_idle_seconds * 1000),
            start_id="0-0",
            count=READ_COUNT,
        )
        # [next start id, claimed entries, ids deleted from the stream meanwhile]
        claimed = cast("list[Entry]", reply[1])
        for message_id, fields in claimed:
            deliveries = await self._deliveries(message_id)
            if deliveries > settings.max_deliveries:
                await self._dead_letter(message_id, fields, deliveries - 1)
            else:
                await self._process(message_id, fields)
        return len(claimed)

    async def _deliveries(self, message_id: str) -> int:
        pending = await self._redis.xpending_range(
            self._settings.stream_name,
            self._settings.consumer_group,
            min=message_id,
            max=message_id,
            count=1,
        )
        return int(pending[0]["times_delivered"]) if pending else 1

    async def _process(self, message_id: str, fields: Mapping[str, str]) -> None:
        topic = fields.get("topic", "")
        handler = self._handlers.get(topic)
        if handler is None:
            # A topic this worker does not know (one a newer version added) is not an error.
            await self._ack(message_id, topic, "ignored")
            return
        try:
            payload = _payload(fields)
        except PermanentError as exc:
            logger.warning(
                "malformed message dropped", extra={"id": message_id, "reason": str(exc)}
            )
            await self._ack(message_id, topic, "dropped")
            return
        started = time.perf_counter()
        try:
            await self._handle(handler, payload)
        except PermanentError as exc:
            logger.warning(
                "message dropped", extra={"id": message_id, "topic": topic, "reason": str(exc)}
            )
            await self._ack(message_id, topic, "dropped")
        except Exception as exc:
            # Not acknowledged: XAUTOCLAIM retries it after claim_idle_seconds.
            logger.warning(
                "message failed; will retry",
                extra={"id": message_id, "topic": topic, "error": f"{type(exc).__name__}: {exc}"},
            )
            self._metrics.messages.labels(topic, "failed").inc()
        else:
            await self._ack(message_id, topic, "processed")
        finally:
            self._metrics.duration.labels(topic).observe(time.perf_counter() - started)

    async def _handle(self, handler: Handler, payload: Mapping[str, Any]) -> None:
        """Run a handler while the heartbeat keeps ticking.

        An analysis can take minutes on a CPU (bounded by the analyser's timeout, as every
        handler is bounded by its own); the worker is busy then, not stuck, and must not
        be reported unhealthy, or restarted, for it.
        """
        ticker = asyncio.create_task(self._tick())
        try:
            await handler.handle(payload)
        finally:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker

    async def _tick(self) -> None:
        while True:
            _touch(self._settings.heartbeat_file)
            await asyncio.sleep(self._settings.heartbeat_interval_seconds)

    async def _dead_letter(self, message_id: str, fields: Mapping[str, str], attempts: int) -> None:
        topic = fields.get("topic", "")
        reason = f"gave up after {attempts} attempts"
        handler = self._handlers.get(topic)
        if handler is not None:
            try:
                await handler.on_exhausted(_payload(fields), reason)
            except Exception:
                logger.exception("recording the failure failed", extra={"id": message_id})
        entry: dict[FieldT, EncodableT] = {"original_id": message_id, "reason": reason}
        for key, value in fields.items():
            entry.setdefault(key, value)
        await self._redis.xadd(
            self._settings.dead_letter_stream_name,
            entry,
            maxlen=self._settings.stream_max_length,
            approximate=True,
        )
        logger.error(
            "message dead-lettered", extra={"id": message_id, "topic": topic, "attempts": attempts}
        )
        await self._ack(message_id, topic, "dead_lettered")

    async def _ack(self, message_id: str, topic: str, outcome: str) -> None:
        await self._redis.xack(
            self._settings.stream_name, self._settings.consumer_group, message_id
        )
        self._metrics.messages.labels(topic or "unknown", outcome).inc()


def _payload(fields: Mapping[str, str]) -> dict[str, Any]:
    try:
        payload = json.loads(fields["payload"])
    except (KeyError, ValueError) as exc:
        msg = "the message has no JSON payload"
        raise PermanentError(msg) from exc
    if not isinstance(payload, dict):
        msg = "the payload is not an object"
        raise PermanentError(msg)
    return payload


def _touch(path: Path) -> None:
    """Heartbeat for the container health check: the loop is alive and reaching Redis."""
    try:
        path.touch()
    except OSError:
        logger.warning("cannot write the heartbeat file", extra={"path": str(path)})
