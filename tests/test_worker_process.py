from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aiops.db.engine import create_database_engine
from aiops.incidents.alertmanager import AlertmanagerWebhook
from aiops.incidents.ingest import IngestPolicy, ingest
from aiops.incidents.models import Incident
from aiops.redis_client import create_redis_client
from aiops.worker.__main__ import run
from aiops.worker.consumer import StreamConsumer
from aiops.worker.metrics import WorkerMetrics
from aiops.worker.relay import OutboxRelay
from tests.conftest import worker_settings
from tests.factories import STARTS_AT, alert, webhook

UNREACHABLE = {
    "db_host": "127.0.0.1",
    "db_port": 9,
    "db_password": SecretStr("unused"),
    "db_password_file": None,
    "db_connect_timeout_seconds": 0.3,
    "redis_host": "127.0.0.1",
    "redis_port": 9,
    "redis_password": None,
    "redis_password_file": None,
    "redis_timeout_seconds": 0.3,
}


async def _stop_after(stop: asyncio.Event, seconds: float) -> None:
    await asyncio.sleep(seconds)
    stop.set()


async def test_the_relay_survives_a_database_outage(tmp_path: Path) -> None:
    settings = worker_settings(tmp_path, relay_poll_seconds=0.05, **UNREACHABLE)
    engine = create_database_engine(settings)
    redis = create_redis_client(settings)
    relay = OutboxRelay(
        async_sessionmaker(engine), redis, settings, WorkerMetrics(CollectorRegistry())
    )
    stop = asyncio.Event()

    await asyncio.wait_for(asyncio.gather(relay.run(stop), _stop_after(stop, 0.5)), timeout=10)

    await redis.aclose()
    await engine.dispose()


async def test_the_consumer_survives_a_redis_outage(tmp_path: Path) -> None:
    settings = worker_settings(tmp_path, **UNREACHABLE)
    redis = create_redis_client(settings)
    consumer = StreamConsumer(redis, settings, {}, WorkerMetrics(CollectorRegistry()), "c")
    stop = asyncio.Event()

    await asyncio.wait_for(asyncio.gather(consumer.run(stop), _stop_after(stop, 0.5)), timeout=10)

    assert not settings.heartbeat_file.exists()  # no heartbeat while Redis is unreachable
    await redis.aclose()


@pytest.mark.integration
async def test_the_worker_process_handles_an_alert_end_to_end(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    settings = worker_settings(tmp_path, metrics_port=0, relay_poll_seconds=0.05)
    payload = AlertmanagerWebhook.model_validate(webhook(alert()))
    policy = IngestPolicy(flap_threshold=3, flap_window=timedelta(hours=1))
    async with sessions() as session, session.begin():
        await ingest(session, payload, policy=policy, now=STARTS_AT)

    stop = asyncio.Event()
    worker = asyncio.create_task(run(settings, stop))
    try:
        for _ in range(100):
            async with sessions() as session:
                incident = await session.scalar(select(Incident))
            if incident is not None and incident.analysis_status != "pending":
                break
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(worker, timeout=10)

    assert incident is not None
    assert incident.context_status == "skipped"  # no Loki configured here
    assert incident.analysis_status == "completed"  # the mock analyser
    assert settings.heartbeat_file.exists()
    redis = create_redis_client(settings)
    for key in (
        settings.stream_name,
        settings.analysis_stream_name,
        settings.critical_analysis_stream_name,
        settings.dead_letter_stream_name,
    ):
        await redis.delete(key)
    await redis.aclose()
