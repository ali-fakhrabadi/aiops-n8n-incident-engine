"""Delivery reports from n8n: the notification's state machine.

    pending --claim--> processing --outcome--> delivered | suppressed | failed

n8n claims a notification before it calls the channel. A second claim is refused, so a
webhook the worker repeated (its answer was lost, say) cannot send the message twice.
Reports are idempotent: the same claim (same n8n execution) or the same outcome again
is accepted without changing anything, which makes n8n's own retries safe.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from aiops.incidents.models import (
    FINAL_NOTIFICATION_STATUSES,
    EventKind,
    Incident,
    IncidentEvent,
    Notification,
    NotificationKind,
    NotificationStatus,
)

_EVENTS = {
    NotificationStatus.DELIVERED: EventKind.NOTIFICATION_DELIVERED,
    NotificationStatus.SUPPRESSED: EventKind.NOTIFICATION_SUPPRESSED,
    NotificationStatus.FAILED: EventKind.NOTIFICATION_FAILED,
}


class UnknownNotificationError(LookupError):
    pass


class DeliveryConflictError(Exception):
    """The report contradicts the notification's state (claimed twice, other outcome)."""


class Outcome(StrEnum):
    CLAIMED = "claimed"
    COMPLETED = "completed"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class DeliveryReport:
    status: NotificationStatus
    route: str | None = None
    channel: str | None = None
    channel_message_id: str | None = None
    error: str | None = None
    execution_id: str | None = None


@dataclass(frozen=True)
class Recorded:
    notification: Notification
    incident: Incident
    outcome: Outcome

    def seconds_since_alert(self) -> float | None:
        """Alert received (or resolved) -> outcome, on the API's clock."""
        completed = self.notification.completed_at
        if completed is None:
            return None
        start = (
            self.incident.first_received_at
            if self.notification.kind == NotificationKind.FIRING
            else self.incident.resolved_at
        )
        return (completed - start).total_seconds() if start is not None else None

    def seconds_in_channel(self) -> float | None:
        """Claimed -> outcome: n8n's routing and the channel call, retries included."""
        claimed, completed = self.notification.claimed_at, self.notification.completed_at
        if claimed is None or completed is None:
            return None
        return (completed - claimed).total_seconds()


async def record_report(
    session: AsyncSession, notification_id: uuid.UUID, report: DeliveryReport, now: datetime
) -> Recorded:
    """Apply one report inside the caller's transaction."""
    notification = await session.get(Notification, notification_id, with_for_update=True)
    if notification is None:
        msg = f"notification {notification_id} does not exist"
        raise UnknownNotificationError(msg)
    incident = await session.get_one(Incident, notification.incident_id)
    current = NotificationStatus(notification.status)

    if report.status == NotificationStatus.PROCESSING:
        if current == NotificationStatus.PENDING:
            notification.status = NotificationStatus.PROCESSING
            notification.claimed_at = now
            notification.execution_id = report.execution_id
            return Recorded(notification, incident, Outcome.CLAIMED)
        if (
            current == NotificationStatus.PROCESSING
            and report.execution_id is not None
            and report.execution_id == notification.execution_id
        ):
            return Recorded(notification, incident, Outcome.UNCHANGED)
        msg = f"the notification is already {current.value}"
        raise DeliveryConflictError(msg)

    if current in FINAL_NOTIFICATION_STATUSES:
        if current == report.status:
            return Recorded(notification, incident, Outcome.UNCHANGED)
        msg = f"the notification is already {current.value}"
        raise DeliveryConflictError(msg)

    notification.status = report.status
    notification.completed_at = now
    notification.route = report.route
    notification.channel = report.channel
    notification.channel_message_id = report.channel_message_id
    notification.error = report.error
    if report.execution_id is not None:
        notification.execution_id = report.execution_id
    recorded = Recorded(notification, incident, Outcome.COMPLETED)
    details: dict[str, Any] = {
        "notification_id": str(notification.id),
        "kind": notification.kind,
        "route": report.route,
        "channel": report.channel,
        "seconds_since_alert": _rounded(recorded.seconds_since_alert()),
        "seconds_in_channel": _rounded(recorded.seconds_in_channel()),
    }
    if report.error:
        details["error"] = report.error
    session.add(
        IncidentEvent(incident_id=incident.id, kind=_EVENTS[report.status], details=details)
    )
    return recorded


def _rounded(seconds: float | None) -> float | None:
    return round(seconds, 3) if seconds is not None else None
