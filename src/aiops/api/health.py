"""Liveness and readiness probes.

Liveness only says the process serves requests, so an orchestrator restarts it only
when it is truly stuck. Readiness checks every dependency a request needs, plus the
migration state: an API whose schema is behind its code must not take traffic.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aiops.api.dependencies import get_engine, get_migration_heads, get_redis, get_settings
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


async def _redis(client: Redis) -> dict[str, CheckStatus]:
    await client.ping()
    return {"redis": "ok"}


async def _bounded(
    name: str,
    check: Awaitable[dict[str, CheckStatus]],
    failed: dict[str, CheckStatus],
    limit_seconds: float,
) -> dict[str, CheckStatus]:
    try:
        async with asyncio.timeout(limit_seconds):
            return await check
    except Exception as exc:  # any failure means "not ready"; the reason goes to the log
        logger.warning("readiness check failed", extra={"check": name, "error": type(exc).__name__})
        return failed


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
    redis: Annotated[Redis, Depends(get_redis)],
    expected_heads: Annotated[frozenset[str], Depends(get_migration_heads)],
) -> ReadinessResponse:
    limit = settings.readiness_timeout_seconds
    results = await asyncio.gather(
        _bounded(
            "database",
            _database(engine, expected_heads),
            {"database": "unavailable", "migrations": "unavailable"},
            limit,
        ),
        _bounded("redis", _redis(redis), {"redis": "unavailable"}, limit),
    )
    checks = {name: state for result in results for name, state in result.items()}
    ok = all(state == "ok" for state in checks.values())
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status="ok" if ok else "unavailable", checks=checks)
