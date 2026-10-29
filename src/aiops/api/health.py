"""Liveness and readiness probes.

Liveness only says the process serves requests, so an orchestrator restarts it only
when it is truly stuck. Readiness checks what a request needs: PostgreSQL, and a schema
at the migration head this build expects. Redis is deliberately not a dependency of the
API: alerts land in the outbox table and reach Redis through the worker, so
Alertmanager deliveries keep succeeding while Redis is down.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aiops.api.dependencies import get_engine, get_migration_heads, get_settings
from aiops.config import Settings
from aiops.db.migrations import current_heads

router = APIRouter(prefix="/health", tags=["health"])
logger = logging.getLogger(__name__)

CheckStatus = Literal["ok", "unavailable", "pending"]


class LivenessResponse(BaseModel):
    status: Literal["ok"]


class ReadinessResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    checks: dict[str, CheckStatus]


async def _database(engine: AsyncEngine, expected: frozenset[str]) -> dict[str, CheckStatus]:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
        applied = await connection.run_sync(current_heads)
    return {"database": "ok", "migrations": "ok" if applied == expected else "pending"}


@router.get("/live", summary="Liveness probe")
async def live() -> LivenessResponse:
    return LivenessResponse(status="ok")


@router.get(
    "/ready",
    summary="Readiness probe",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def ready(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    engine: Annotated[AsyncEngine, Depends(get_engine)],
    expected_heads: Annotated[frozenset[str], Depends(get_migration_heads)],
) -> ReadinessResponse:
    checks: dict[str, CheckStatus]
    try:
        async with asyncio.timeout(settings.readiness_timeout_seconds):
            checks = await _database(engine, expected_heads)
    except Exception as exc:  # any failure means "not ready"; the reason goes to the log
        logger.warning(
            "readiness check failed", extra={"check": "database", "error": type(exc).__name__}
        )
        checks = {"database": "unavailable", "migrations": "unavailable"}
    ok = all(state == "ok" for state in checks.values())
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status="ok" if ok else "unavailable", checks=checks)
