"""Read API for incidents: list with filters and pagination, and detail with timeline."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aiops.api.auth import require_api_token
from aiops.api.dependencies import get_session_factory
from aiops.incidents.models import IncidentStatus, Severity
from aiops.incidents.queries import (
    IncidentFilter,
    InvalidCursorError,
    get_incident,
    list_incidents,
)
from aiops.incidents.schemas import (
    IncidentDetail,
    IncidentEventOut,
    IncidentPage,
    IncidentSummary,
)

router = APIRouter(
    prefix="/api/v1/incidents",
    tags=["incidents"],
    dependencies=[Depends(require_api_token)],
    responses={status.HTTP_401_UNAUTHORIZED: {"description": "Missing or wrong API token"}},
)

Sessions = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


class IncidentListQuery(BaseModel):
    """Query parameters of the listing; unknown parameters are rejected, not ignored."""

    model_config = ConfigDict(extra="forbid")

    status: IncidentStatus | None = None
    severity: Severity | None = None
    alertname: str | None = Field(default=None, max_length=200)
    flapping: bool | None = None
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, max_length=512)


@router.get("", summary="List incidents, newest first")
async def list_(sessions: Sessions, query: Annotated[IncidentListQuery, Query()]) -> IncidentPage:
    filters = IncidentFilter(
        status=query.status,
        severity=query.severity,
        alertname=query.alertname,
        flapping=query.flapping,
    )
    async with sessions() as session:
        try:
            rows, next_cursor = await list_incidents(
                session, filters, limit=query.limit, cursor=query.cursor
            )
        except InvalidCursorError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return IncidentPage(
        items=[IncidentSummary.model_validate(row) for row in rows], next_cursor=next_cursor
    )


@router.get(
    "/{incident_id}",
    summary="One incident with its timeline and collected log context",
    responses={status.HTTP_404_NOT_FOUND: {"description": "No such incident"}},
)
async def detail(incident_id: uuid.UUID, sessions: Sessions) -> IncidentDetail:
    async with sessions() as session:
        found = await get_incident(session, incident_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No such incident.")
    incident, events = found
    summary = IncidentSummary.model_validate(incident)
    return IncidentDetail(
        **summary.model_dump(),
        fingerprint=incident.fingerprint,
        annotations=incident.annotations,
        generator_url=incident.generator_url,
        context=incident.context,
        context_updated_at=incident.context_updated_at,
        events=[IncidentEventOut.model_validate(event) for event in events],
    )
