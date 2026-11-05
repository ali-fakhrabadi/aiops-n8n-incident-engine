"""The worker's stages wired like the worker process, driven one pass at a time."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import httpx
from prometheus_client import CollectorRegistry
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aiops.analysis.providers import Analyzer, MockAnalyzer
from aiops.config import WorkerSettings
from aiops.incidents.alertmanager import AlertmanagerWebhook
from aiops.incidents.ingest import IngestPolicy, ingest
from aiops.incidents.models import Incident, IncidentEvent, Notification, OutboxMessage
from aiops.incidents.topics import TOPIC_ANALYZED, TOPIC_RESOLVED
from aiops.logcontext.loki import LokiClient
from aiops.notifications.client import N8nClient, RetryPolicy
from aiops.notifications.signing import Signer
from aiops.worker.consumer import StreamConsumer
from aiops.worker.handlers import AnalyzeIncident, CollectLogContext, Handler
from aiops.worker.metrics import WorkerMetrics
from aiops.worker.notify import NotifyIncident
from aiops.worker.relay import OutboxRelay
from tests.factories import STARTS_AT, webhook

Sessions = async_sessionmaker[AsyncSession]
LOKI = "http://loki.test:3100"
QUERY_RANGE = f"{LOKI}/loki/api/v1/query_range"
N8N_WEBHOOK = "http://n8n.test:5678/webhook/aiops-incident"
WEBHOOK_SECRET = "test-webhook-secret-0123456789abcdef"
NOW = STARTS_AT + timedelta(minutes=30)


class Pipeline:
    """Relay, consumer and every handler, as in the worker process.

    With `notify`, the notification stage posts to N8N_WEBHOOK (answered by respx in the
    tests that use it) and does not wait between attempts; `sleeps` records the delays.
    """

    def __init__(
        self,
        sessions: Sessions,
        redis: Redis,
        settings: WorkerSettings,
        loki: bool,
        analyzer: Analyzer | None = None,
        *,
        notify: bool = False,
    ) -> None:
        self.sessions = sessions
        self.redis = redis
        self.settings = settings
        self.registry = CollectorRegistry()
        self.metrics = WorkerMetrics(self.registry)
        self.sleeps: list[float] = []
        # No sockets are opened: respx answers for Loki and n8n inside the tests.
        self.http = httpx.AsyncClient()
        client = LokiClient(LOKI, self.http) if loki else None
        n8n = (
            N8nClient(
                N8N_WEBHOOK,
                Signer(SecretStr(WEBHOOK_SECRET), ttl_seconds=300, clock=lambda: NOW),
                self.http,
                RetryPolicy(attempts=3, backoff_seconds=2.0),
                sleep=self._sleep,
            )
            if notify
            else None
        )
        handlers: list[Handler] = [
            CollectLogContext(sessions, client, settings, self.metrics, clock=lambda: NOW),
            AnalyzeIncident(
                sessions, analyzer or MockAnalyzer(), settings, self.metrics, clock=lambda: NOW
            ),
            NotifyIncident(TOPIC_ANALYZED, sessions, n8n, self.metrics, clock=lambda: NOW),
            NotifyIncident(TOPIC_RESOLVED, sessions, n8n, self.metrics, clock=lambda: NOW),
        ]
        self.relay = OutboxRelay(sessions, redis, settings, self.metrics)
        self.consumer = StreamConsumer(
            redis, settings, {h.topic: h for h in handlers}, self.metrics, "test-consumer"
        )

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    async def deliver(self, *alerts: dict[str, Any], minutes: int = 1) -> list[Incident]:
        payload = AlertmanagerWebhook.model_validate(webhook(*alerts))
        policy = IngestPolicy(flap_threshold=3, flap_window=timedelta(hours=1))
        async with self.sessions() as session, session.begin():
            await ingest(
                session, payload, policy=policy, now=STARTS_AT + timedelta(minutes=minutes)
            )
        async with self.sessions() as session:
            return list((await session.scalars(select(Incident))).all())

    async def run(self) -> int:
        """One pass: publish the outbox, then handle what is due."""
        await self.relay.publish_batch()
        await self.consumer.ensure_group()
        return await self.consumer.run_once()

    async def run_until_idle(self, passes: int = 10) -> None:
        """Pass after pass until nothing is left to do (each stage adds the next event)."""
        for _ in range(passes):
            if await self.run() == 0:
                return

    async def incident(self, incident_id: object) -> Incident:
        async with self.sessions() as session:
            return await session.get_one(Incident, incident_id)

    async def notifications(self, incident_id: object) -> list[Notification]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(Notification)
                .where(Notification.incident_id == incident_id)
                .order_by(Notification.created_at, Notification.kind)
            )
            return list(rows.all())

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
