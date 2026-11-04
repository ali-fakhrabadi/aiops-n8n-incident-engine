"""The worker's stages wired like the worker process, driven one pass at a time."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import httpx
from prometheus_client import CollectorRegistry
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aiops.analysis.providers import Analyzer, MockAnalyzer
from aiops.config import WorkerSettings
from aiops.incidents.alertmanager import AlertmanagerWebhook
from aiops.incidents.ingest import IngestPolicy, ingest
from aiops.incidents.models import Incident, IncidentEvent, OutboxMessage
from aiops.logcontext.loki import LokiClient
from aiops.worker.consumer import StreamConsumer
from aiops.worker.handlers import AnalyzeIncident, CollectLogContext, Handler
from aiops.worker.metrics import WorkerMetrics
from aiops.worker.relay import OutboxRelay
from tests.factories import STARTS_AT, webhook

Sessions = async_sessionmaker[AsyncSession]
LOKI = "http://loki.test:3100"
QUERY_RANGE = f"{LOKI}/loki/api/v1/query_range"
NOW = STARTS_AT + timedelta(minutes=30)


class Pipeline:
    """Relay, consumer, log-context and analysis handlers, as in the worker process."""

    def __init__(
        self,
        sessions: Sessions,
        redis: Redis,
        settings: WorkerSettings,
        loki: bool,
        analyzer: Analyzer | None = None,
    ) -> None:
        self.sessions = sessions
        self.redis = redis
        self.settings = settings
        self.registry = CollectorRegistry()
        self.metrics = WorkerMetrics(self.registry)
        # No sockets are opened: respx answers for Loki inside the tests that use it.
        self.http = httpx.AsyncClient()
        client = LokiClient(LOKI, self.http) if loki else None
        handlers: list[Handler] = [
            CollectLogContext(sessions, client, settings, self.metrics, clock=lambda: NOW),
            AnalyzeIncident(
                sessions, analyzer or MockAnalyzer(), settings, self.metrics, clock=lambda: NOW
            ),
        ]
        self.relay = OutboxRelay(sessions, redis, settings, self.metrics)
        self.consumer = StreamConsumer(
            redis, settings, {h.topic: h for h in handlers}, self.metrics, "test-consumer"
        )

    async def deliver(self, *alerts: dict[str, Any]) -> list[Incident]:
        payload = AlertmanagerWebhook.model_validate(webhook(*alerts))
        policy = IngestPolicy(flap_threshold=3, flap_window=timedelta(hours=1))
        async with self.sessions() as session, session.begin():
            await ingest(session, payload, policy=policy, now=STARTS_AT + timedelta(minutes=1))
        async with self.sessions() as session:
            return list((await session.scalars(select(Incident))).all())

    async def run(self) -> int:
        """One pass: publish the outbox, then handle what is due."""
        await self.relay.publish_batch()
        await self.consumer.ensure_group()
        return await self.consumer.run_once()

    async def incident(self, incident_id: object) -> Incident:
        async with self.sessions() as session:
            return await session.get_one(Incident, incident_id)

    async def events(self, incident_id: object) -> list[str]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(IncidentEvent.kind)
                .where(IncidentEvent.incident_id == incident_id)
                .order_by(IncidentEvent.id)
            )
            return list(rows.all())

    async def outbox_topics(self) -> list[str]:
        async with self.sessions() as session:
            rows = await session.scalars(select(OutboxMessage.topic).order_by(OutboxMessage.id))
            return list(rows.all())

    def counter(self, name: str, **labels: str) -> float:
        return self.registry.get_sample_value(name, labels) or 0.0

    async def close(self) -> None:
        await self.http.aclose()


async def entries(redis: Redis, stream: str) -> list[tuple[str, dict[str, str]]]:
    """XRANGE with the shape redis-py returns when decode_responses is on."""
    return cast("list[tuple[str, dict[str, str]]]", await redis.xrange(stream))


def loki_lines(*lines: str) -> dict[str, object]:
    base = int(STARTS_AT.timestamp() * 1_000_000_000)
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {
                        "container": "shop-api-1",
                        "compose_service": "api",
                        "level": "error",
                    },
                    "values": [[str(base + i), line] for i, line in enumerate(lines)],
                }
            ],
        },
    }
