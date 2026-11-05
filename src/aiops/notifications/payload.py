"""What n8n receives about an incident (payload version 1).

Only the checked parts of an analysis leave the engine: commands that passed the
read-only allowlist and evidence found in the logs or the alert. Rejected commands and
invented quotes stay in the incident record for review. Every text field may still
contain whatever the monitored systems wrote, so the workflow escapes it before it
reaches a chat.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from aiops.incidents.models import AnalysisStatus, Incident, IncidentStatus, Notification

PAYLOAD_VERSION = 1


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _analysis(incident: Incident) -> dict[str, Any]:
    stored = incident.analysis or {}
    analysis: dict[str, Any] = {"status": incident.analysis_status}
    if incident.analysis_status == AnalysisStatus.COMPLETED:
        analysis.update(
            {
                "provider": stored.get("provider"),
                "model": stored.get("model"),
                "duration_ms": stored.get("duration_ms"),
                "confidence": stored.get("confidence"),
                "summary": stored.get("summary"),
                "probable_cause": stored.get("probable_cause"),
                "evidence": stored.get("evidence", []),
                "suggested_commands": stored.get("suggested_commands", []),
                "next_steps": stored.get("next_steps", []),
                "commands_rejected": len(stored.get("rejected_commands", [])),
            }
        )
    elif incident.analysis_status == AnalysisStatus.FAILED:
        analysis.update(
            {
                "provider": stored.get("provider"),
                "model": stored.get("model"),
                "reason": stored.get("reason"),
            }
        )
    return analysis


def notification_payload(incident: Incident, notification: Notification) -> dict[str, Any]:
    resolved = notification.reported_status == IncidentStatus.RESOLVED
    return {
        "version": PAYLOAD_VERSION,
        "notification": {
            "id": str(notification.id),
            "kind": notification.kind,
            "created_at": _iso(notification.created_at),
        },
        "incident": {
            "id": str(incident.id),
            "alertname": incident.alertname,
            "severity": incident.severity,
            # As of this message: a resent firing message stays a firing message, and the
            # all-clear that follows it reports the resolution.
            "status": notification.reported_status,
            "flapping": incident.flapping,
            "starts_at": _iso(incident.starts_at),
            "resolved_at": _iso(incident.resolved_at) if resolved else None,
            "first_received_at": _iso(incident.first_received_at),
            "labels": incident.labels,
            "annotations": incident.annotations,
            "generator_url": incident.generator_url,
        },
        "analysis": _analysis(incident),
    }
