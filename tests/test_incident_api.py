from __future__ import annotations

import uuid
from datetime import timedelta

import httpx
import pytest

from aiops.errors import PROBLEM_MEDIA_TYPE
from tests.conftest import INGEST_TOKEN
from tests.factories import STARTS_AT, alert, webhook

pytestmark = pytest.mark.integration

ALERTS_URL = "/api/v1/alerts/alertmanager"
URL = "/api/v1/incidents"


async def _seed(api: httpx.AsyncClient, ingest_headers: dict[str, str]) -> None:
    """Five incidents, delivered oldest first."""
    specs = [
        ("ContainerRestarting", "warning", "firing", "0000000000000001"),
        ("ContainerRestarting", "warning", "resolved", "0000000000000002"),
        ("TargetDown", "critical", "firing", "0000000000000003"),
        ("HostDiskSpaceLow", "warning", "firing", "0000000000000004"),
        ("TargetDown", "critical", "firing", "0000000000000005"),
    ]
    for index, (name, severity, status, fingerprint) in enumerate(specs):
        starts = STARTS_AT + timedelta(minutes=index)
        ends = starts + timedelta(minutes=1) if status == "resolved" else None
        item = alert(
            name,
            severity=severity,
            status=status,
            fingerprint=fingerprint,
            starts_at=starts,
            ends_at=ends,
        )
        response = await api.post(ALERTS_URL, json=webhook(item), headers=ingest_headers)
        assert response.status_code == 200


async def test_the_api_needs_its_own_token(api: httpx.AsyncClient) -> None:
    anonymous = await api.get(URL)
    with_ingest_token = await api.get(URL, headers={"Authorization": f"Bearer {INGEST_TOKEN}"})

    assert anonymous.status_code == with_ingest_token.status_code == 401
    assert anonymous.headers["www-authenticate"] == 'Bearer realm="api"'


async def test_listing_is_newest_first_with_filters(
    api: httpx.AsyncClient, ingest_headers: dict[str, str], api_headers: dict[str, str]
) -> None:
    await _seed(api, ingest_headers)

    everything = (await api.get(URL, headers=api_headers)).json()
    critical = (await api.get(URL, params={"severity": "critical"}, headers=api_headers)).json()
    resolved = (await api.get(URL, params={"status": "resolved"}, headers=api_headers)).json()
    by_name = (
        await api.get(URL, params={"alertname": "HostDiskSpaceLow"}, headers=api_headers)
    ).json()

    assert [item["alertname"] for item in everything["items"]] == [
        "TargetDown",
        "HostDiskSpaceLow",
        "TargetDown",
        "ContainerRestarting",
        "ContainerRestarting",
    ]
    assert everything["next_cursor"] is None
    assert {item["severity"] for item in critical["items"]} == {"critical"}
    assert len(critical["items"]) == 2
    assert [item["status"] for item in resolved["items"]] == ["resolved"]
    assert len(by_name["items"]) == 1


async def test_cursor_pagination_visits_every_incident_once(
    api: httpx.AsyncClient, ingest_headers: dict[str, str], api_headers: dict[str, str]
) -> None:
    await _seed(api, ingest_headers)

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = (await api.get(URL, params=params, headers=api_headers)).json()
        seen.extend(item["id"] for item in page["items"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert pages == 3
    assert len(seen) == len(set(seen)) == 5


async def test_a_misspelled_filter_is_rejected_not_ignored(
    api: httpx.AsyncClient, api_headers: dict[str, str]
) -> None:
    response = await api.get(URL, params={"sevrity": "critical"}, headers=api_headers)

    assert response.status_code == 422
    assert response.json()["errors"][0]["loc"] == ["query", "sevrity"]


async def test_a_forged_cursor_is_a_bad_request(
    api: httpx.AsyncClient, api_headers: dict[str, str]
) -> None:
    response = await api.get(URL, params={"cursor": "bm90LWEtY3Vyc29y"}, headers=api_headers)

    assert response.status_code == 400
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE


async def test_detail_shows_the_timeline(
    api: httpx.AsyncClient, ingest_headers: dict[str, str], api_headers: dict[str, str]
) -> None:
    await _seed(api, ingest_headers)
    resolved = (await api.get(URL, params={"status": "resolved"}, headers=api_headers)).json()
    incident_id = resolved["items"][0]["id"]

    detail = (await api.get(f"{URL}/{incident_id}", headers=api_headers)).json()

    assert detail["fingerprint"] == "0000000000000002"
    assert detail["annotations"]["summary"] == "ContainerRestarting on shop/api"
    assert detail["context_status"] == "skipped"
    assert detail["context"] is None
    assert [event["kind"] for event in detail["events"]] == ["opened", "resolved"]


async def test_unknown_and_malformed_ids(
    api: httpx.AsyncClient, api_headers: dict[str, str]
) -> None:
    missing = await api.get(f"{URL}/{uuid.uuid4()}", headers=api_headers)
    malformed = await api.get(f"{URL}/not-a-uuid", headers=api_headers)

    assert missing.status_code == 404
    assert malformed.status_code == 422
