from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aiops.errors import PROBLEM_MEDIA_TYPE
from aiops.incidents.models import Incident, IncidentEvent, OutboxMessage
from tests.conftest import INGEST_TOKEN, offline_settings, serve
from tests.factories import STARTS_AT, alert, webhook

URL = "/api/v1/alerts/alertmanager"
Sessions = async_sessionmaker[AsyncSession]


async def _incidents(sessions: Sessions) -> list[Incident]:
    async with sessions() as session:
        return list((await session.scalars(select(Incident).order_by(Incident.starts_at))).all())


async def _events(sessions: Sessions) -> list[tuple[str, dict[str, object]]]:
    async with sessions() as session:
        rows = (await session.scalars(select(IncidentEvent).order_by(IncidentEvent.id))).all()
    return [(row.kind, row.details) for row in rows]


async def _outbox(sessions: Sessions) -> list[str]:
    async with sessions() as session:
        rows = (await session.scalars(select(OutboxMessage).order_by(OutboxMessage.id))).all()
    return [row.topic for row in rows]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token-0123456789"},
        {"Authorization": f"Basic {INGEST_TOKEN}"},
    ],
)
async def test_delivery_needs_the_ingest_token(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    response = await client.post(URL, json=webhook(alert()), headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer realm="ingest"'


async def test_invalid_delivery_is_a_problem_without_echo(
    client: httpx.AsyncClient, ingest_headers: dict[str, str]
) -> None:
    payload = webhook(alert())
    payload["alerts"][0]["startsAt"] = "secret-looking-not-a-date"

    response = await client.post(URL, json=payload, headers=ingest_headers)

    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert "secret-looking-not-a-date" not in response.text


async def test_too_many_alerts_are_refused(ingest_headers: dict[str, str]) -> None:
    alerts = [alert(fingerprint=f"{i:016x}") for i in range(3)]
    async with serve(offline_settings(max_alerts_per_request=2)) as client:
        response = await client.post(URL, json=webhook(*alerts), headers=ingest_headers)

    assert response.status_code == 413


async def test_database_outage_asks_alertmanager_to_retry(
    client: httpx.AsyncClient, ingest_headers: dict[str, str]
) -> None:
    response = await client.post(URL, json=webhook(alert()), headers=ingest_headers)

    assert response.status_code == 503
    assert response.json()["detail"] == "The database is unavailable; retry."


@pytest.mark.integration
async def test_a_firing_alert_opens_an_incident(
    api: httpx.AsyncClient, sessions: Sessions, ingest_headers: dict[str, str]
) -> None:
    response = await api.post(URL, json=webhook(alert()), headers=ingest_headers)

    assert response.status_code == 200
    assert response.json() == {
        "received": 1,
        "opened": 1,
        "updated": 0,
        "resolved": 0,
        "ignored": 0,
        "flapping": 0,
    }
    [incident] = await _incidents(sessions)
    assert incident.alertname == "ContainerRestarting"
    assert incident.severity == "warning"
    assert incident.status == "firing"
    assert incident.starts_at == STARTS_AT
    assert incident.labels["compose_service"] == "api"
    assert incident.notification_count == 1
    assert incident.context_status == "pending"
    assert not incident.flapping
    assert await _events(sessions) == [("opened", {"receiver": "aiops", "severity": "warning"})]
    assert await _outbox(sessions) == ["incident.opened"]


@pytest.mark.integration
async def test_lifecycle_repeat_resolve_and_late_retries(
    api: httpx.AsyncClient, sessions: Sessions, ingest_headers: dict[str, str]
) -> None:
    resolved_at = STARTS_AT + timedelta(minutes=7)
    deliveries = [
        alert(),  # fires
        alert(),  # repeated at the next group interval
        alert(status="resolved", ends_at=resolved_at),  # resolves
        alert(status="resolved", ends_at=resolved_at),  # the resolution, delivered twice
        alert(),  # a retried firing delivery arriving late
    ]
    outcomes = []
    for delivery in deliveries:
        response = await api.post(URL, json=webhook(delivery), headers=ingest_headers)
        body = response.json()
        outcomes.append(next(key for key in ("opened", "updated", "resolved") if body[key]))

    assert outcomes == ["opened", "updated", "resolved", "updated", "updated"]
    [incident] = await _incidents(sessions)
    assert incident.status == "resolved"  # never reopened by the late retry
    assert incident.ends_at == resolved_at
    assert incident.notification_count == 5
    assert [kind for kind, _ in await _events(sessions)] == ["opened", "resolved"]
    assert (await _events(sessions))[1][1] == {"duration_seconds": 420.0}
    assert await _outbox(sessions) == ["incident.opened", "incident.resolved"]


@pytest.mark.integration
async def test_a_new_start_time_is_a_new_incident(
    api: httpx.AsyncClient, sessions: Sessions, ingest_headers: dict[str, str]
) -> None:
    await api.post(URL, json=webhook(alert()), headers=ingest_headers)
    later = STARTS_AT + timedelta(hours=2)
    await api.post(URL, json=webhook(alert(starts_at=later)), headers=ingest_headers)

    incidents = await _incidents(sessions)

    assert [incident.starts_at for incident in incidents] == [STARTS_AT, later]
    assert len({incident.fingerprint for incident in incidents}) == 1


@pytest.mark.integration
async def test_resolved_on_arrival_needs_no_investigation(
    api: httpx.AsyncClient, sessions: Sessions, ingest_headers: dict[str, str]
) -> None:
    delivery = alert(status="resolved", ends_at=STARTS_AT + timedelta(minutes=1))
    await api.post(URL, json=webhook(delivery), headers=ingest_headers)

    [incident] = await _incidents(sessions)
    assert incident.status == "resolved"
    assert incident.context_status == "skipped"
    assert incident.analysis_status == "skipped"
    assert [kind for kind, _ in await _events(sessions)] == ["opened", "resolved"]
    assert await _outbox(sessions) == []


@pytest.mark.integration
async def test_heartbeat_alerts_are_ignored(
    api: httpx.AsyncClient, sessions: Sessions, ingest_headers: dict[str, str]
) -> None:
    response = await api.post(
        URL, json=webhook(alert("Watchdog", severity="none")), headers=ingest_headers
    )

    assert response.json()["ignored"] == 1
    assert await _incidents(sessions) == []


@pytest.mark.integration
async def test_an_alert_that_keeps_reopening_is_flagged_as_flapping(
    api: httpx.AsyncClient, sessions: Sessions, ingest_headers: dict[str, str]
) -> None:
    flags = []
    for minutes in (0, 10, 20):
        starts = STARTS_AT + timedelta(minutes=minutes)
        response = await api.post(
            URL, json=webhook(alert(starts_at=starts)), headers=ingest_headers
        )
        flags.append(response.json()["flapping"])

    assert flags == [0, 0, 1]
    incidents = await _incidents(sessions)
    assert [incident.flapping for incident in incidents] == [False, False, True]
    _, details = (await _events(sessions))[-1]
    assert details["flapping"] == {"occurrences": 3, "window_minutes": 60}


@pytest.mark.integration
async def test_concurrent_duplicate_deliveries_open_one_incident(
    api: httpx.AsyncClient, sessions: Sessions, ingest_headers: dict[str, str]
) -> None:
    # An Alertmanager HA pair delivers the same alert from every replica at once.
    batch = webhook(alert(), alert("HighMemory", fingerprint="ffffeeeeddddcccc"))
    responses = await asyncio.gather(
        *(api.post(URL, json=batch, headers=ingest_headers) for _ in range(5))
    )

    assert {response.status_code for response in responses} == {200}
    assert sum(response.json()["opened"] for response in responses) == 2
    incidents = await _incidents(sessions)
    assert len(incidents) == 2
    assert {incident.notification_count for incident in incidents} == {5}
    async with sessions() as session:
        outbox = await session.scalar(select(func.count()).select_from(OutboxMessage))
    assert outbox == 2
