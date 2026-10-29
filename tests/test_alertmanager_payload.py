from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from aiops.incidents.alertmanager import AlertmanagerWebhook
from aiops.incidents.models import Severity
from tests.factories import STARTS_AT, alert, webhook


def _parse(payload: dict[str, Any]) -> AlertmanagerWebhook:
    return AlertmanagerWebhook.model_validate(payload)


def test_a_real_shaped_delivery_is_parsed() -> None:
    parsed = _parse(webhook(alert()))

    item = parsed.alerts[0]
    assert parsed.receiver == "aiops"
    assert item.alertname == "ContainerRestarting"
    assert item.severity is Severity.WARNING
    assert item.starts_at == STARTS_AT
    assert item.ends_at is None  # Go's zero time means "not set"
    assert item.identity == "a1b2c3d4e5f60718"
    assert item.generator_url.startswith("http://prometheus:9090/")


def test_unknown_severity_is_kept_as_unknown() -> None:
    parsed = _parse(webhook(alert(severity="page-everyone")))

    assert parsed.alerts[0].severity is Severity.UNKNOWN


def test_identity_without_fingerprint_is_stable_for_the_label_set() -> None:
    first = alert(fingerprint="", labels={"instance": "web-1", "job": "node"})
    second = alert(fingerprint="", labels={"job": "node", "instance": "web-1"})
    other = alert(fingerprint="", labels={"instance": "web-2", "job": "node"})

    identities = [_parse(webhook(item)).alerts[0].identity for item in (first, second, other)]

    assert identities[0] == identities[1] != identities[2]
    assert len(identities[0]) == 16


def test_naive_timestamps_are_read_as_utc() -> None:
    payload = alert()
    payload["startsAt"] = "2026-10-29T09:00:00"

    parsed = _parse(webhook(payload))

    assert parsed.alerts[0].starts_at == datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p["alerts"][0]["labels"].pop("alertname"), "alertname"),
        (lambda p: p["alerts"][0].update(fingerprint="not-hex!"), "fingerprint"),
        (lambda p: p.update(version="3"), "version"),
        (lambda p: p.update(alerts=[]), "alerts"),
        (
            lambda p: p["alerts"][0]["labels"].update({f"l{i}": "v" for i in range(70)}),
            "labels",
        ),
        (lambda p: p["alerts"][0]["labels"].update(host="x" * 2000), "labels"),
    ],
)
def test_invalid_deliveries_are_rejected(mutate: Any, message: str) -> None:
    payload = webhook(alert())
    mutate(payload)

    with pytest.raises(ValidationError, match=message):
        _parse(payload)
