"""Alertmanager payloads for tests, shaped like real version 4 deliveries."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

STARTS_AT = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
GO_ZERO_TIME = "0001-01-01T00:00:00Z"


def alert(
    alertname: str = "ContainerRestarting",
    *,
    status: str = "firing",
    starts_at: datetime = STARTS_AT,
    ends_at: datetime | None = None,
    severity: str = "warning",
    fingerprint: str = "a1b2c3d4e5f60718",
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "labels": {
            "alertname": alertname,
            "severity": severity,
            "compose_project": "shop",
            "compose_service": "api",
            **(labels or {}),
        },
        "annotations": {
            "summary": f"{alertname} on shop/api",
            "runbook_url": "https://example.com/rb",
        },
        "startsAt": starts_at.isoformat().replace("+00:00", "Z"),
        "endsAt": ends_at.isoformat().replace("+00:00", "Z") if ends_at else GO_ZERO_TIME,
        "generatorURL": "http://prometheus:9090/graph?g0.expr=up",
        "fingerprint": fingerprint,
    }


def webhook(*alerts: dict[str, Any], receiver: str = "aiops") -> dict[str, Any]:
    statuses = {item["status"] for item in alerts}
    return {
        "version": "4",
        "groupKey": '{}:{alertname="ContainerRestarting"}',
        "truncatedAlerts": 0,
        "status": "firing" if "firing" in statuses else "resolved",
        "receiver": receiver,
        "groupLabels": {"alertname": alerts[0]["labels"]["alertname"]} if alerts else {},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
        "alerts": list(alerts),
    }
