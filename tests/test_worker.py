from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from redis.asyncio import Redis
from sqlalchemy import update

from aiops.incidents.models import OutboxMessage
from aiops.worker.handlers import CollectLogContext
from tests.conftest import worker_settings
from tests.factories import STARTS_AT, alert
from tests.pipeline import LOKI, NOW, QUERY_RANGE, Pipeline, Sessions, entries, loki_lines

pytestmark = pytest.mark.integration


@pytest.fixture
async def pipeline(sessions: Sessions, redis: Redis, tmp_path: Path) -> AsyncIterator[Pipeline]:
    built = Pipeline(sessions, redis, worker_settings(tmp_path, loki_url=LOKI), loki=True)
    yield built
    await built.close()


@pytest.fixture
async def pipeline_without_loki(
    sessions: Sessions, redis: Redis, tmp_path: Path
) -> AsyncIterator[Pipeline]:
    built = Pipeline(sessions, redis, worker_settings(tmp_path), loki=False)
    yield built
    await built.close()


async def test_relay_publishes_each_outbox_event_once(
    pipeline_without_loki: Pipeline, redis: Redis
) -> None:
    pipeline = pipeline_without_loki
    [incident] = await pipeline.deliver(alert())

    first = await pipeline.relay.publish_batch()
    second = await pipeline.relay.publish_batch()

    assert (first, second) == (1, 0)
    published = await entries(redis, pipeline.settings.stream_name)
    assert len(published) == 1
    _, fields = published[0]
    assert fields["topic"] == "incident.opened"
    assert json.loads(fields["payload"]) == {"incident_id": str(incident.id)}
    assert pipeline.counter("aiops_outbox_published_total") == 1


async def test_relay_prunes_old_published_events(
    pipeline_without_loki: Pipeline, sessions: Sessions
) -> None:
    pipeline = pipeline_without_loki
    await pipeline.deliver(alert())
    await pipeline.relay.publish_batch()
    async with sessions() as session, session.begin():
        await session.execute(
            update(OutboxMessage).values(published_at=datetime.now(UTC) - timedelta(days=8))
        )

    assert await pipeline.relay.prune(datetime.now(UTC)) == 1


@respx.mock
async def test_log_context_is_collected_and_redacted(pipeline: Pipeline) -> None:
    route = respx.get(QUERY_RANGE).mock(
        return_value=httpx.Response(
            200,
            json=loki_lines(
                "starting worker",
                "ERROR db connect failed: postgres://app:hunter2@db:5432/shop",
                "panic: retries exhausted",
            ),
        )
    )
    [incident] = await pipeline.deliver(alert())

    assert await pipeline.run() == 1

    stored = await pipeline.incident(incident.id)
    assert stored.context_status == "collected"
    assert stored.context is not None
    assert stored.context["selector"] == '{compose_project="shop", compose_service="api"}'
    lines = [line["line"] for line in stored.context["lines"]]
    assert lines[0] == "starting worker"
    assert "hunter2" not in lines[1]
    assert lines[2] == "panic: retries exhausted"
    # Window: 15 minutes before the start to 2 minutes after.
    params = route.calls.last.request.url.params
    assert int(params["start"]) == int((STARTS_AT - timedelta(minutes=15)).timestamp() * 1e9)
    assert int(params["end"]) == int((STARTS_AT + timedelta(minutes=2)).timestamp() * 1e9)
    assert await pipeline.events(incident.id) == ["opened", "context_collected"]
    assert pipeline.counter("aiops_context_collections_total", status="collected") == 1
    assert (
        pipeline.counter(
            "aiops_worker_messages_total", topic="incident.opened", outcome="processed"
        )
        == 1
    )
    assert await pipeline.redis.xpending(pipeline.settings.stream_name, "test-workers") == {
        "pending": 0,
        "min": None,
        "max": None,
        "consumers": [],
    }


@respx.mock
async def test_no_lines_in_the_window_is_recorded_as_empty(pipeline: Pipeline) -> None:
    respx.get(QUERY_RANGE).mock(return_value=httpx.Response(200, json=loki_lines()))
    [incident] = await pipeline.deliver(alert())

    await pipeline.run()

    assert (await pipeline.incident(incident.id)).context_status == "empty"


async def test_alerts_without_log_labels_are_skipped(pipeline: Pipeline) -> None:
    host_alert = alert("HostDiskSpaceLow")
    host_alert["labels"] = {
        "alertname": "HostDiskSpaceLow",
        "instance": "host",
        "severity": "warning",
    }
    [incident] = await pipeline.deliver(host_alert)

    await pipeline.run()

    stored = await pipeline.incident(incident.id)
    assert stored.context_status == "skipped"
    assert stored.context == {"reason": "no log stream matches the alert's labels"}


async def test_without_loki_collection_is_skipped(pipeline_without_loki: Pipeline) -> None:
    pipeline = pipeline_without_loki
    [incident] = await pipeline.deliver(alert())

    await pipeline.run()

    stored = await pipeline.incident(incident.id)
    assert stored.context == {"reason": "Loki is not configured"}


@respx.mock
async def test_a_failing_loki_is_retried_until_it_answers(pipeline: Pipeline) -> None:
    route = respx.get(QUERY_RANGE).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=loki_lines("recovered"))]
    )
    [incident] = await pipeline.deliver(alert())

    await pipeline.run()  # attempt 1: Loki fails, the message stays pending
    assert (await pipeline.incident(incident.id)).context_status == "pending"
    await pipeline.run()  # attempt 2: claimed again (claim_idle_seconds=0) and succeeds

    assert route.call_count == 2
    assert (await pipeline.incident(incident.id)).context_status == "collected"
    assert (
        pipeline.counter("aiops_worker_messages_total", topic="incident.opened", outcome="failed")
        == 1
    )


@respx.mock
async def test_exhausted_retries_end_in_the_dead_letter_stream(
    sessions: Sessions, redis: Redis, tmp_path: Path
) -> None:
    respx.get(QUERY_RANGE).mock(return_value=httpx.Response(503))
    settings = worker_settings(tmp_path, loki_url=LOKI, max_deliveries=2)
    pipeline = Pipeline(sessions, redis, settings, loki=True)
    [incident] = await pipeline.deliver(alert())

    try:
        for _ in range(3):  # two attempts, then the third claim gives up
            await pipeline.run()
    finally:
        await pipeline.close()

    stored = await pipeline.incident(incident.id)
    assert stored.context_status == "failed"
    assert stored.context == {"reason": "gave up after 2 attempts"}
    assert await pipeline.events(incident.id) == ["opened", "context_failed"]
    dead = await entries(redis, settings.dead_letter_stream_name)
    assert len(dead) == 1
    assert dead[0][1]["reason"] == "gave up after 2 attempts"
    assert (await redis.xpending(settings.stream_name, "test-workers"))["pending"] == 0


async def test_unhandled_and_malformed_messages_are_acknowledged(pipeline: Pipeline) -> None:
    stream = pipeline.settings.stream_name
    await pipeline.consumer.ensure_group()
    await pipeline.redis.xadd(stream, {"topic": "incident.resolved", "payload": "{}"})
    await pipeline.redis.xadd(stream, {"topic": "incident.opened", "payload": "not json"})
    await pipeline.redis.xadd(
        stream, {"topic": "incident.opened", "payload": '{"incident_id": "nope"}'}
    )

    assert await pipeline.consumer.run_once() == 3

    assert (await pipeline.redis.xpending(stream, "test-workers"))["pending"] == 0
    assert (
        pipeline.counter(
            "aiops_worker_messages_total", topic="incident.resolved", outcome="ignored"
        )
        == 1
    )
    assert (
        pipeline.counter("aiops_worker_messages_total", topic="incident.opened", outcome="dropped")
        == 2
    )


@respx.mock
async def test_redelivery_of_finished_work_changes_nothing(pipeline: Pipeline) -> None:
    respx.get(QUERY_RANGE).mock(return_value=httpx.Response(200, json=loki_lines("one")))
    [incident] = await pipeline.deliver(alert())
    await pipeline.run()

    handler = CollectLogContext(
        pipeline.sessions, None, pipeline.settings, pipeline.metrics, clock=lambda: NOW
    )
    await handler.handle({"incident_id": str(incident.id)})  # the same message again

    assert (await pipeline.incident(incident.id)).context_status == "collected"
    assert await pipeline.events(incident.id) == ["opened", "context_collected"]
