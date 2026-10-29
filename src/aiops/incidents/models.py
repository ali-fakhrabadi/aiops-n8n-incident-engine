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


class EventKind(StrEnum):
    OPENED = "opened"
    RESOLVED = "resolved"
    CONTEXT_COLLECTED = "context_collected"
    CONTEXT_FAILED = "context_failed"


def _in(values: type[StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in values)


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        UniqueConstraint("fingerprint", "starts_at", name="uq_incidents_fingerprint_starts_at"),
        CheckConstraint(f"status IN ({_in(IncidentStatus)})", name="status"),
        CheckConstraint(f"severity IN ({_in(Severity)})", name="severity"),
        CheckConstraint(f"context_status IN ({_in(ContextStatus)})", name="context_status"),
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
