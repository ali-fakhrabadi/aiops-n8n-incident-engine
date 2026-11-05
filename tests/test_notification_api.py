"""n8n's delivery reports: claim once, report once, and only with the callback token."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from aiops.incidents.models import Incident, Notification
from tests.conftest import METRICS_TOKEN
from tests.factories import alert, webhook
from tests.pipeline import Sessions

pytestmark = pytest.mark.integration


async def _report(
    api: httpx.AsyncClient, notification_id: uuid.UUID, headers: dict[str, str], **body: object
) -> httpx.Response:
    return await api.post(
        f"/api/v1/notifications/{notification_id}/delivery", json=body, headers=headers
    )


@pytest.fixture
async def notification(
    api: httpx.AsyncClient, sessions: Sessions, ingest_headers: dict[str, str]
) -> Notification:
    """A firing notification the worker has handed to n8n."""
    response = await api.post(
        "/api/v1/alerts/alertmanager",
        json=webhook(alert(severity="critical")),
        headers=ingest_headers,
    )
    assert response.status_code == 200
    async with sessions() as session, session.begin():
        incident = await session.scalar(select(Incident))
        assert incident is not None
        row = Notification(
            id=uuid.uuid4(),
            incident_id=incident.id,
            kind="firing",
            status="pending",
            reported_status="firing",
            created_at=datetime.now(UTC),
            handed_off_at=datetime.now(UTC),
            handoff_attempts=1,
        )
        session.add(row)
    return row


async def test_n8n_claims_then_reports_the_delivery(
    api: httpx.AsyncClient,
    notification: Notification,
    callback_headers: dict[str, str],
    api_headers: dict[str, str],
) -> None:
    claim = await _report(
        api, notification.id, callback_headers, status="processing", execution_id="41"
    )
    assert claim.status_code == 200
    assert claim.json()["status"] == "processing"
    assert claim.json()["claimed_at"] is not None

    done = await _report(
        api,
        notification.id,
        callback_headers,
        status="delivered",
        route="page",
        channel="telegram",
        channel_message_id=1234,  # Telegram's message_id is a number
        execution_id="41",
    )
    assert done.status_code == 200
    assert done.json()["status"] == "delivered"
    assert done.json()["channel_message_id"] == "1234"

    detail = (
        await api.get(f"/api/v1/incidents/{notification.incident_id}", headers=api_headers)
    ).json()
    [listed] = detail["notifications"]
    assert (listed["status"], listed["route"], listed["channel"]) == (
        "delivered",
        "page",
        "telegram",
    )
    event = detail["events"][-1]
    assert event["kind"] == "notification_delivered"
    assert event["details"]["route"] == "page"
    assert event["details"]["seconds_since_alert"] >= 0
    assert event["details"]["seconds_in_channel"] >= 0

    metrics = (await api.get("/metrics", headers={"Authorization": f"Bearer {METRICS_TOKEN}"})).text
    assert 'aiops_notifications_total{kind="firing",route="page",status="delivered"} 1.0' in metrics
    assert 'aiops_alert_to_notification_seconds_count{kind="firing"} 1.0' in metrics
    assert (
        'aiops_notification_channel_seconds_count{channel="telegram",status="delivered"} 1.0'
        in metrics
    )


async def test_a_second_claim_is_refused(
    api: httpx.AsyncClient, notification: Notification, callback_headers: dict[str, str]
) -> None:
    first = await _report(
        api, notification.id, callback_headers, status="processing", execution_id="41"
    )
    again = await _report(
        api, notification.id, callback_headers, status="processing", execution_id="41"
    )
    other = await _report(
        api, notification.id, callback_headers, status="processing", execution_id="42"
    )

    assert (first.status_code, again.status_code, other.status_code) == (200, 200, 409)
    assert other.headers["content-type"] == "application/problem+json"
    assert other.json()["detail"] == "the notification is already processing."


async def test_outcome_reports_are_idempotent(
    api: httpx.AsyncClient,
    notification: Notification,
    callback_headers: dict[str, str],
    api_headers: dict[str, str],
) -> None:
    await _report(api, notification.id, callback_headers, status="processing", execution_id="41")
    first = await _report(
        api, notification.id, callback_headers, status="failed", error="Telegram: 502"
    )
    again = await _report(
        api, notification.id, callback_headers, status="failed", error="Telegram: 502"
    )
    other = await _report(api, notification.id, callback_headers, status="delivered")

    assert (first.status_code, again.status_code, other.status_code) == (200, 200, 409)
    detail = (
        await api.get(f"/api/v1/incidents/{notification.incident_id}", headers=api_headers)
    ).json()
    kinds = [event["kind"] for event in detail["events"]]
    assert kinds.count("notification_failed") == 1
    assert detail["notifications"][0]["error"] == "Telegram: 502"


async def test_routing_may_suppress_without_a_claim(
    api: httpx.AsyncClient, notification: Notification, callback_headers: dict[str, str]
) -> None:
    response = await _report(
        api, notification.id, callback_headers, status="suppressed", route="suppress"
    )

    assert response.status_code == 200
    assert response.json()["status"] == "suppressed"
    assert response.json()["claimed_at"] is None


async def test_an_unknown_notification_is_not_found(
    api: httpx.AsyncClient, callback_headers: dict[str, str]
) -> None:
    response = await _report(api, uuid.uuid4(), callback_headers, status="processing")

    assert response.status_code == 404


async def test_only_the_callback_token_may_report(
    api: httpx.AsyncClient, notification: Notification, api_headers: dict[str, str]
) -> None:
    with_api_token = await _report(api, notification.id, api_headers, status="processing")
    without = await _report(api, notification.id, {}, status="processing")

    assert (with_api_token.status_code, without.status_code) == (401, 401)
    assert with_api_token.headers["www-authenticate"] == 'Bearer realm="callback"'


@pytest.mark.parametrize(
    "body",
    [
        {"status": "sent"},
        {"status": "delivered", "route": "Page!"},
        {"status": "delivered", "colour": "red"},
        {"status": "failed", "error": "x" * 2001},
    ],
)
async def test_malformed_reports_are_rejected(
    api: httpx.AsyncClient,
    notification: Notification,
    callback_headers: dict[str, str],
    body: dict[str, object],
) -> None:
    response = await _report(api, notification.id, callback_headers, **body)

    assert response.status_code == 422
