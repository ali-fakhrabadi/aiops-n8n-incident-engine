"""Response models of the incident API (the OpenAPI schema is generated from these)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from aiops.incidents.models import ContextStatus, IncidentStatus, Severity


class IngestResponse(BaseModel):
    received: int
    opened: int
    updated: int
    resolved: int
    ignored: int
    flapping: int


class IncidentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    alertname: str
    severity: Severity
    status: IncidentStatus
    flapping: bool
    labels: dict[str, str]
    starts_at: datetime
    ends_at: datetime | None
    first_received_at: datetime
    last_received_at: datetime
    resolved_at: datetime | None
    notification_count: int
    context_status: ContextStatus


class IncidentEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kind: str
    occurred_at: datetime
    details: dict[str, Any]


class IncidentDetail(IncidentSummary):
    fingerprint: str
    annotations: dict[str, str]
    generator_url: str | None
    context: dict[str, Any] | None
    context_updated_at: datetime | None
    events: list[IncidentEventOut]


class IncidentPage(BaseModel):
    items: list[IncidentSummary]
    next_cursor: str | None
