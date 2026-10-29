from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from tests.conftest import live_settings, serve


async def test_liveness_needs_no_dependencies(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_reports_unreachable_dependencies(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"database": "unavailable", "migrations": "unavailable"},
    }


@pytest.mark.integration
async def test_readiness_with_live_dependencies(migrated: None) -> None:
    async with serve(live_settings()) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"database": "ok", "migrations": "ok"},
    }


@pytest.mark.integration
async def test_readiness_reports_pending_migrations() -> None:
    def expect_unknown_revision(app: FastAPI) -> None:
        app.state.migration_heads = frozenset({"0123456789ab"})

    async with serve(live_settings(), expect_unknown_revision) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["migrations"] == "pending"
