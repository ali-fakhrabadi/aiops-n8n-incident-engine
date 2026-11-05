"""The notification stage in the pipeline: one firing message, one all-clear, no repeats."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import respx
from redis.asyncio import Redis

from aiops.analysis.providers import Analyzer, AnalyzerError
from aiops.incidents.models import Notification
from aiops.incidents.topics import TOPIC_ANALYZED
from aiops.notifications.signing import AUDIENCE, ISSUER
from aiops.worker.notify import NotifyIncident
from tests.conftest import worker_settings
from tests.factories import STARTS_AT, alert
from tests.pipeline import N8N_WEBHOOK, NOW, WEBHOOK_SECRET, Pipeline, Sessions, entries
from tests.test_analysis_pipeline import BlockingAnalyzer, StubAnalyzer

pytestmark = pytest.mark.integration

MakePipeline = Callable[..., Pipeline]
ACCEPTED = httpx.Response(202, json={"accepted": True})


def _resolved() -> dict[str, Any]:
    return alert(status="resolved", ends_at=STARTS_AT + timedelta(minutes=5))


@pytest.fixture
async def make_pipeline(
    sessions: Sessions, redis: Redis, tmp_path: Path
) -> AsyncIterator[MakePipeline]:
    built: list[Pipeline] = []

    def make(
        analyzer: Analyzer | None = None, *, notify: bool = True, **overrides: Any
    ) -> Pipeline:
        settings = worker_settings(tmp_path, **overrides)
        pipeline = Pipeline(sessions, redis, settings, False, analyzer, notify=notify)
        built.append(pipeline)
        return pipeline

    yield make
    for pipeline in built:
        await pipeline.close()


def _received(request: httpx.Request) -> tuple[dict[str, Any], dict[str, Any]]:
    """The token's claims and the payload, checked the way the n8n workflow checks them."""
    token = request.headers["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(
        token,
        WEBHOOK_SECRET,
        algorithms=["HS256"],
        audience=AUDIENCE,
        issuer=ISSUER,
        options={"verify_exp": False},  # the tests' clock is fixed in the past
    )
    text = json.loads(request.content)["payload"]
    assert hashlib.sha256(text.encode()).hexdigest() == claims["payload_sha256"]
    return claims, json.loads(text)


@respx.mock
async def test_an_analysed_incident_is_handed_to_n8n(make_pipeline: MakePipeline) -> None:
    route = respx.post(N8N_WEBHOOK).mock(return_value=ACCEPTED)
    pipeline = make_pipeline()
    [incident] = await pipeline.deliver(alert(severity="warning"))

    await pipeline.run_until_idle()

    [notification] = await pipeline.notifications(incident.id)
    assert notification.kind == "firing"
    assert notification.reported_status == "firing"
    assert notification.status == "pending"  # until n8n claims it
    assert notification.handed_off_at == NOW
    assert notification.handoff_attempts == 1
    claims, payload = _received(route.calls.last.request)
    assert claims["sub"] == str(incident.id)
    assert claims["jti"] == str(notification.id)
    assert payload["notification"] == {
        "id": str(notification.id),
        "kind": "firing",
        "created_at": NOW.isoformat(),
        "follows_page": False,
        "reply_to_message_id": None,
    }
    assert payload["incident"]["severity"] == "warning"
    assert payload["incident"]["resolved_at"] is None
    analysis = payload["analysis"]
    assert analysis["status"] == "completed"
    assert analysis["provider"] == "mock"
    # Only checked output leaves the engine.
    assert "rejected_commands" not in analysis
    assert "dropped_evidence" not in analysis
    assert await pipeline.events(incident.id) == [
        "opened",
        "context_collected",
        "analysis_completed",
        "notification_sent",
    ]
    handoffs = pipeline.counter(
        "aiops_notification_handoffs_total", kind="firing", outcome="accepted"
    )
    assert handoffs == 1


@respx.mock
async def test_the_all_clear_follows_the_firing_message(make_pipeline: MakePipeline) -> None:
    route = respx.post(N8N_WEBHOOK).mock(return_value=ACCEPTED)
    pipeline = make_pipeline()
    [incident] = await pipeline.deliver(alert())
    await pipeline.run_until_idle()

    await pipeline.deliver(_resolved(), minutes=6)
    await pipeline.run_until_idle()

    firing, resolved = await pipeline.notifications(incident.id)
    assert (firing.kind, firing.reported_status) == ("firing", "firing")
    assert (resolved.kind, resolved.reported_status) == ("resolved", "resolved")
    assert route.call_count == 2
    claims, payload = _received(route.calls.last.request)
    assert claims["jti"] == str(resolved.id)
    assert payload["incident"]["status"] == "resolved"
    assert payload["incident"]["resolved_at"] is not None


@respx.mock
async def test_an_alert_resolved_before_its_analysis_gets_one_message(
    make_pipeline: MakePipeline,
) -> None:
    route = respx.post(N8N_WEBHOOK).mock(return_value=ACCEPTED)
    pipeline = make_pipeline()
    [incident] = await pipeline.deliver(alert())
    await pipeline.deliver(_resolved(), minutes=6)

    await pipeline.run_until_idle()

    [notification] = await pipeline.notifications(incident.id)
    assert (notification.kind, notification.reported_status) == ("firing", "resolved")
    assert route.call_count == 1
    _, payload = _received(route.calls.last.request)
    assert payload["incident"]["status"] == "resolved"

    # The all-clear event coming (again) after that message changes nothing either.
    payload_text = json.dumps({"incident_id": str(incident.id)})
    await pipeline.redis.xadd(
        pipeline.settings.stream_name, {"topic": "incident.resolved", "payload": payload_text}
    )
    await pipeline.run_until_idle()
    assert route.call_count == 1
    assert len(await pipeline.notifications(incident.id)) == 1


async def test_a_message_about_an_unknown_incident_is_dropped(make_pipeline: MakePipeline) -> None:
    pipeline = make_pipeline()
    payload_text = json.dumps({"incident_id": str(uuid.uuid4())})
    await pipeline.consumer.ensure_group()
    await pipeline.redis.xadd(
        pipeline.settings.stream_name, {"topic": "incident.analyzed", "payload": payload_text}
    )

    await pipeline.run_until_idle()

    dropped = pipeline.counter(
        "aiops_worker_messages_total", topic="incident.analyzed", outcome="dropped"
    )
    assert dropped == 1


async def test_giving_up_without_a_notification_records_nothing(
    make_pipeline: MakePipeline,
) -> None:
    pipeline = make_pipeline(notify=False)
    [incident] = await pipeline.deliver(alert())
    handler = NotifyIncident(TOPIC_ANALYZED, pipeline.sessions, None, pipeline.metrics)

    await handler.on_exhausted({"incident_id": str(incident.id)}, "gave up after 5 attempts")

    assert await pipeline.notifications(incident.id) == []
    assert await pipeline.events(incident.id) == ["opened"]


@respx.mock
async def test_a_failed_analysis_is_still_notified(make_pipeline: MakePipeline) -> None:
    route = respx.post(N8N_WEBHOOK).mock(return_value=ACCEPTED)
    pipeline = make_pipeline(StubAnalyzer(AnalyzerError("the answer did not match the schema")))
    [incident] = await pipeline.deliver(alert())

    await pipeline.run_until_idle()

    _, payload = _received(route.calls.last.request)
    assert payload["analysis"] == {
        "status": "failed",
        "provider": "stub",
        "model": "stub-1",
        "reason": "the answer did not match the schema",
    }
    assert (await pipeline.notifications(incident.id))[0].handed_off_at is not None


@respx.mock
async def test_n8n_refusing_the_request_fails_the_notification(make_pipeline: MakePipeline) -> None:
    respx.post(N8N_WEBHOOK).mock(
        return_value=httpx.Response(403, json={"code": 403, "message": "invalid signature"})
    )
    pipeline = make_pipeline()
    [incident] = await pipeline.deliver(alert())

    await pipeline.run_until_idle()

    [notification] = await pipeline.notifications(incident.id)
    assert notification.status == "failed"
    assert notification.error == "n8n answered 403: invalid signature"
    assert notification.handoff_attempts == 1
    assert (await pipeline.events(incident.id))[-1] == "notification_failed"
    assert (await pipeline.redis.xpending(pipeline.settings.stream_name, "test-workers"))[
        "pending"
    ] == 0


@respx.mock
async def test_a_retry_sends_the_same_notification_again(make_pipeline: MakePipeline) -> None:
    route = respx.post(N8N_WEBHOOK).mock(
        side_effect=[httpx.Response(503), httpx.Response(503), httpx.Response(503), ACCEPTED]
    )
    pipeline = make_pipeline()
    [incident] = await pipeline.deliver(alert())

    await pipeline.run_until_idle()

    [notification] = await pipeline.notifications(incident.id)
    assert notification.handed_off_at is not None
    assert notification.handoff_attempts == 4
    assert {_received(call.request)[0]["jti"] for call in route.calls} == {str(notification.id)}
    assert pipeline.sleeps == [2.0, 4.0]  # the second delivery succeeded at once
    assert (
        pipeline.counter("aiops_worker_messages_total", topic="incident.analyzed", outcome="failed")
        == 1
    )


@respx.mock
async def test_n8n_down_for_good_ends_as_a_failed_notification(
    make_pipeline: MakePipeline,
) -> None:
    respx.post(N8N_WEBHOOK).mock(return_value=httpx.Response(503, text="Service Unavailable"))
    pipeline = make_pipeline(max_deliveries=2)
    [incident] = await pipeline.deliver(alert())

    await pipeline.run_until_idle()

    [notification] = await pipeline.notifications(incident.id)
    assert notification.status == "failed"
    assert notification.error == "gave up after 2 attempts"
    assert notification.handoff_attempts == 6  # 2 deliveries x 3 attempts
    dead = await entries(pipeline.redis, pipeline.settings.dead_letter_stream_name)
    assert [fields["topic"] for _, fields in dead] == ["incident.analyzed"]


@respx.mock
async def test_a_redelivered_message_does_not_notify_twice(make_pipeline: MakePipeline) -> None:
    route = respx.post(N8N_WEBHOOK).mock(return_value=ACCEPTED)
    pipeline = make_pipeline()
    [incident] = await pipeline.deliver(alert())
    await pipeline.run_until_idle()

    payload = json.dumps({"incident_id": str(incident.id)})
    await pipeline.redis.xadd(
        pipeline.settings.stream_name, {"topic": "incident.analyzed", "payload": payload}
    )
    await pipeline.run_until_idle()

    assert route.call_count == 1
    assert len(await pipeline.notifications(incident.id)) == 1


async def test_without_n8n_nothing_is_sent(make_pipeline: MakePipeline) -> None:
    pipeline = make_pipeline(notify=False)
    [incident] = await pipeline.deliver(alert())

    await pipeline.run_until_idle()

    assert await pipeline.notifications(incident.id) == []
    assert (
        pipeline.counter(
            "aiops_worker_messages_total", topic="incident.analyzed", outcome="processed"
        )
        == 1
    )


async def _delivered(pipeline: Pipeline, notification_id: uuid.UUID, message_id: str) -> None:
    """What n8n's delivery report would record."""
    async with pipeline.sessions() as session, session.begin():
        row = await session.get_one(Notification, notification_id)
        row.status = "delivered"
        row.channel_message_id = message_id


def _by_kind(rows: list[Notification]) -> dict[str, Notification]:
    return {row.kind: row for row in rows}


@respx.mock
async def test_a_critical_alert_pages_before_its_analysis(make_pipeline: MakePipeline) -> None:
    route = respx.post(N8N_WEBHOOK).mock(return_value=ACCEPTED)
    analyzer = BlockingAnalyzer()
    pipeline = make_pipeline(analyzer)
    [incident] = await pipeline.deliver(alert("TargetDown", severity="critical"))

    await pipeline.relay.publish_batch()
    await pipeline.consumer.ensure_group()
    await pipeline.consumer.run_once()  # main lane only: log context and the page

    [page] = await pipeline.notifications(incident.id)
    assert (page.kind, page.reported_status, page.handed_off_at) == ("page", "firing", NOW)
    claims, payload = _received(route.calls.last.request)
    assert claims["jti"] == str(page.id)
    assert payload["notification"]["kind"] == "page"
    assert payload["analysis"] == {"status": "pending"}
    assert not analyzer.started.is_set()  # nobody waited for the model

    await _delivered(pipeline, page.id, "42")
    analyzer.release.set()
    await pipeline.run_until_idle()

    sent = _by_kind(await pipeline.notifications(incident.id))
    assert set(sent) == {"page", "firing"}
    _, follow_up = _received(route.calls.last.request)
    assert follow_up["notification"]["kind"] == "firing"
    assert follow_up["notification"]["follows_page"] is True
    assert follow_up["notification"]["reply_to_message_id"] == "42"
    assert follow_up["analysis"]["status"] == "completed"


@respx.mock
async def test_a_page_resolved_before_the_analysis_gets_its_all_clear_at_once(
    make_pipeline: MakePipeline,
) -> None:
    route = respx.post(N8N_WEBHOOK).mock(return_value=ACCEPTED)
    analyzer = BlockingAnalyzer()
    pipeline = make_pipeline(analyzer)
    [incident] = await pipeline.deliver(alert("TargetDown", severity="critical"))
    await pipeline.relay.publish_batch()
    await pipeline.consumer.ensure_group()
    await pipeline.consumer.run_once()  # the page goes out

    await pipeline.deliver(
        alert(
            "TargetDown",
            severity="critical",
            status="resolved",
            ends_at=STARTS_AT + timedelta(minutes=3),
        ),
        minutes=4,
    )
    await pipeline.relay.publish_batch()
    await pipeline.consumer.run_once()  # the all-clear, the analysis still blocked

    sent = _by_kind(await pipeline.notifications(incident.id))
    assert set(sent) == {"page", "resolved"}
    assert sent["resolved"].reported_status == "resolved"

    analyzer.release.set()
    await pipeline.run_until_idle()  # the analysis follows, for the record

    sent = _by_kind(await pipeline.notifications(incident.id))
    assert set(sent) == {"page", "resolved", "firing"}
    assert sent["firing"].reported_status == "resolved"
    assert route.call_count == 3


@respx.mock
async def test_a_page_is_sent_once(make_pipeline: MakePipeline) -> None:
    route = respx.post(N8N_WEBHOOK).mock(return_value=ACCEPTED)
    pipeline = make_pipeline()
    [incident] = await pipeline.deliver(alert("TargetDown", severity="critical"))
    await pipeline.run_until_idle()
    calls = route.call_count

    payload = json.dumps({"incident_id": str(incident.id)})
    await pipeline.redis.xadd(
        pipeline.settings.stream_name, {"topic": "incident.page", "payload": payload}
    )
    await pipeline.run_until_idle()

    assert route.call_count == calls == 2  # the page and the analysis
    kinds = sorted(row.kind for row in await pipeline.notifications(incident.id))
    assert kinds == ["firing", "page"]


@respx.mock
async def test_resolved_before_the_page_went_out_the_analysis_says_so(
    make_pipeline: MakePipeline,
) -> None:
    route = respx.post(N8N_WEBHOOK).mock(return_value=ACCEPTED)
    pipeline = make_pipeline()
    [incident] = await pipeline.deliver(alert("TargetDown", severity="critical"))
    await pipeline.deliver(
        alert(
            "TargetDown",
            severity="critical",
            status="resolved",
            ends_at=STARTS_AT + timedelta(minutes=1),
        ),
        minutes=2,
    )

    await pipeline.run_until_idle()

    [notification] = await pipeline.notifications(incident.id)
    assert (notification.kind, notification.reported_status) == ("firing", "resolved")
    assert route.call_count == 1  # no page for an alert already over, no separate all-clear
