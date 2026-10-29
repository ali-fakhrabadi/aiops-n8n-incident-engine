"""Transactional outbox relay: committed events -> Redis stream.

Rows are locked with SKIP LOCKED, so several worker replicas share the work without
publishing a row twice. A crash between XADD and the commit publishes those rows again
on the next pass (at-least-once); consumers are idempotent for exactly that reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aiops.config import WorkerSettings
from aiops.incidents.models import OutboxMessage
from aiops.worker.metrics import WorkerMetrics

logger = logging.getLogger(__name__)

PRUNE_INTERVAL = timedelta(hours=1)
RETENTION = timedelta(days=7)
MAX_BACKOFF_SECONDS = 30.0


class OutboxRelay:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis: Redis,
        settings: WorkerSettings,
        metrics: WorkerMetrics,
    ) -> None:
        self._sessions = sessions
        self._redis = redis
        self._settings = settings
        self._metrics = metrics
        self._last_prune = datetime.min.replace(tzinfo=UTC)

    async def publish_batch(self) -> int:
        """Publish up to one batch of pending events; returns how many."""
        settings = self._settings
        async with self._sessions() as session, session.begin():
            rows = (
                await session.scalars(
                    select(OutboxMessage)
                    .where(OutboxMessage.published_at.is_(None))
                    .order_by(OutboxMessage.id)
                    .limit(settings.relay_batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            if not rows:
                return 0
            async with self._redis.pipeline(transaction=False) as pipe:
                for row in rows:
                    pipe.xadd(
                        settings.stream_name,
                        {
                            "event_id": str(row.id),
                            "topic": row.topic,
                            "payload": json.dumps(row.payload),
                        },
                        maxlen=settings.stream_max_length,
                        approximate=True,
                    )
                await pipe.execute()
            published_at = datetime.now(UTC)
            for row in rows:
                row.published_at = published_at
        self._metrics.published.inc(len(rows))
        return len(rows)

    async def prune(self, now: datetime) -> int:
        """Delete events published more than RETENTION ago."""
        async with self._sessions() as session, session.begin():
            result = await session.scalars(
                delete(OutboxMessage)
                .where(OutboxMessage.published_at < now - RETENTION)
                .returning(OutboxMessage.id)
            )
            return len(result.all())

    async def run(self, stop: asyncio.Event) -> None:
        backoff = self._settings.relay_poll_seconds
        while not stop.is_set():
            try:
                published = await self.publish_batch()
                now = datetime.now(UTC)
                if now - self._last_prune >= PRUNE_INTERVAL:
                    self._last_prune = now
                    pruned = await self.prune(now)
                    if pruned:
                        logger.info("outbox pruned", extra={"rows": pruned})
                backoff = self._settings.relay_poll_seconds
            except Exception:
                logger.exception(
                    "outbox relay failed; retrying", extra={"backoff_seconds": backoff}
                )
                published = 0
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            if published < self._settings.relay_batch_size:
                # Idle (or failing): wait, but wake up at once on shutdown.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
