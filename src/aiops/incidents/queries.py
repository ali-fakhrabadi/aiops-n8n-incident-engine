"""Read side: filtered, keyset-paginated incident listing and incident detail."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from aiops.incidents.models import Incident, IncidentEvent, IncidentStatus, Notification, Severity


class InvalidCursorError(ValueError):
    pass


@dataclass(frozen=True)
class IncidentFilter:
    status: IncidentStatus | None = None
    severity: Severity | None = None
    alertname: str | None = None
    flapping: bool | None = None


def encode_cursor(incident: Incident) -> str:
    raw = json.dumps([incident.first_received_at.isoformat(), str(incident.id)])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        received, incident_id = json.loads(base64.urlsafe_b64decode(padded))
        return datetime.fromisoformat(received), uuid.UUID(incident_id)
    except (binascii.Error, ValueError, TypeError) as exc:
        msg = "the cursor is not one this API returned"
        raise InvalidCursorError(msg) from exc


async def list_incidents(
    session: AsyncSession, filters: IncidentFilter, *, limit: int, cursor: str | None
) -> tuple[Sequence[Incident], str | None]:
    """Newest first. Keyset pagination stays fast and stable while incidents arrive."""
    query = select(Incident)
    if filters.status is not None:
        query = query.where(Incident.status == filters.status)
    if filters.severity is not None:
        query = query.where(Incident.severity == filters.severity)
    if filters.alertname is not None:
        query = query.where(Incident.alertname == filters.alertname)
    if filters.flapping is not None:
        query = query.where(Incident.flapping.is_(filters.flapping))
    if cursor is not None:
        received, incident_id = decode_cursor(cursor)
        query = query.where(
            tuple_(Incident.first_received_at, Incident.id) < tuple_(received, incident_id)
        )
    query = query.order_by(Incident.first_received_at.desc(), Incident.id.desc()).limit(limit + 1)
    rows = (await session.scalars(query)).all()
    page, more = rows[:limit], len(rows) > limit
    return page, encode_cursor(page[-1]) if more else None


@dataclass(frozen=True)
class IncidentRecord:
    incident: Incident
    notifications: Sequence[Notification]
    events: Sequence[IncidentEvent]


async def get_incident(session: AsyncSession, incident_id: uuid.UUID) -> IncidentRecord | None:
    incident = await session.get(Incident, incident_id)
    if incident is None:
        return None
    notifications = (
        await session.scalars(
            select(Notification)
            .where(Notification.incident_id == incident_id)
            .order_by(Notification.created_at, Notification.id)
        )
    ).all()
    events = (
        await session.scalars(
            select(IncidentEvent)
            .where(IncidentEvent.incident_id == incident_id)
            .order_by(IncidentEvent.id)
        )
    ).all()
    return IncidentRecord(incident, notifications, events)
