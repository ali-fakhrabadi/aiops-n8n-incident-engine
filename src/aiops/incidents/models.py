"""Incident tables.

An incident is one occurrence of one alert: Alertmanager's fingerprint (the alert's label
set) plus the time it started firing. Alertmanager re-sends a firing alert on every
group interval and again when it resolves; all of those deliveries update the same row.
When the alert fires again later, it has a new start time and opens a new incident.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aiops.db.base import Base


class IncidentStatus(StrEnum):
    FIRING = "firing"
    RESOLVED = "resolved"


class Severity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"
    NONE = "none"
    UNKNOWN = "unknown"


class ContextStatus(StrEnum):
    PENDING = "pending"  # waiting for the worker
    COLLECTED = "collected"  # log lines stored
    EMPTY = "empty"  # the matching streams had no lines in the window
    SKIPPED = "skipped"  # nothing to collect (no Loki, no matching labels, resolved on arrival)
    FAILED = "failed"  # Loki kept failing until the retries ran out


class AnalysisStatus(StrEnum):
    PENDING = "pending"  # waiting for the log context, then the analyser
    COMPLETED = "completed"  # an analysis is stored
    FAILED = "failed"  # the analyser rejected the request or kept failing
    SKIPPED = "skipped"  # nothing to analyse (resolved on arrival)


class NotificationKind(StrEnum):
    FIRING = "firing"  # sent once the analysis is done (or has failed)
    RESOLVED = "resolved"  # the all-clear, unless the firing message already reported it


class NotificationStatus(StrEnum):
    PENDING = "pending"  # recorded; the worker is handing it to n8n
    PROCESSING = "processing"  # n8n claimed it and is sending it
    DELIVERED = "delivered"  # the channel accepted the message
    SUPPRESSED = "suppressed"  # n8n's routing decided not to send it
    FAILED = "failed"  # n8n or the channel kept failing


FINAL_NOTIFICATION_STATUSES = frozenset(
    {NotificationStatus.DELIVERED, NotificationStatus.SUPPRESSED, NotificationStatus.FAILED}
)


class EventKind(StrEnum):
    OPENED = "opened"
    RESOLVED = "resolved"
    CONTEXT_COLLECTED = "context_collected"
    CONTEXT_FAILED = "context_failed"
    ANALYSIS_COMPLETED = "analysis_completed"
    ANALYSIS_FAILED = "analysis_failed"
    NOTIFICATION_SENT = "notification_sent"  # n8n accepted the webhook
    NOTIFICATION_DELIVERED = "notification_delivered"
    NOTIFICATION_SUPPRESSED = "notification_suppressed"
    NOTIFICATION_FAILED = "notification_failed"


def _in(values: type[StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in values)


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        UniqueConstraint("fingerprint", "starts_at", name="uq_incidents_fingerprint_starts_at"),
        CheckConstraint(f"status IN ({_in(IncidentStatus)})", name="status"),
        CheckConstraint(f"severity IN ({_in(Severity)})", name="severity"),
        CheckConstraint(f"context_status IN ({_in(ContextStatus)})", name="context_status"),
        CheckConstraint(f"analysis_status IN ({_in(AnalysisStatus)})", name="analysis_status"),
        CheckConstraint("notification_count >= 1", name="notification_count"),
        # Newest-first listing with keyset pagination.
        Index("ix_incidents_first_received_at_id", "first_received_at", "id"),
        Index("ix_incidents_status_severity", "status", "severity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    alertname: Mapped[str] = mapped_column(String(200))
    severity: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    labels: Mapped[dict[str, str]] = mapped_column(JSONB)
    annotations: Mapped[dict[str, str]] = mapped_column(JSONB)
    generator_url: Mapped[str | None] = mapped_column(Text)
    first_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notification_count: Mapped[int] = mapped_column(Integer)
    flapping: Mapped[bool] = mapped_column(Boolean)
    context_status: Mapped[str] = mapped_column(String(16))
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    context_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    analysis_status: Mapped[str] = mapped_column(String(16))
    analysis: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    analysis_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IncidentEvent(Base):
    """Append-only timeline of an incident."""

    __tablename__ = "incident_events"
    __table_args__ = (Index("ix_incident_events_incident_id_id", "incident_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class Notification(Base):
    """One message about an incident, handed to n8n and reported back by it.

    At most one per incident and kind. The row is the delivery's state machine: the
    worker creates it before calling n8n, n8n claims it before sending (so a repeated
    webhook cannot send twice) and reports the outcome.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("incident_id", "kind", name="uq_notifications_incident_id_kind"),
        CheckConstraint(f"kind IN ({_in(NotificationKind)})", name="kind"),
        CheckConstraint(f"status IN ({_in(NotificationStatus)})", name="status"),
        CheckConstraint(f"reported_status IN ({_in(IncidentStatus)})", name="reported_status"),
        CheckConstraint("handoff_attempts >= 0", name="handoff_attempts"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    # The incident status the message reports: a firing message sent after the alert
    # resolved says so, and then no separate all-clear follows.
    reported_status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    handed_off_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    handoff_attempts: Mapped[int] = mapped_column(Integer, default=0)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    route: Mapped[str | None] = mapped_column(String(32))
    channel: Mapped[str | None] = mapped_column(String(32))
    channel_message_id: Mapped[str | None] = mapped_column(String(64))
    execution_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)


class OutboxMessage(Base):
    """Events committed in the same transaction as the change they describe.

    The worker's relay publishes them to the Redis stream, so an event is never lost
    when Redis is down and never sent for a change that was rolled back.
    """

    __tablename__ = "outbox"
    __table_args__ = (
        Index("ix_outbox_unpublished", "id", postgresql_where=text("published_at IS NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    topic: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
