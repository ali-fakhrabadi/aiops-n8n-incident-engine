"""Shared fixtures.

Unit tests build settings that point at closed ports, so they never need PostgreSQL or
Redis. Integration tests (marked `integration`) take connection settings from the
AIOPS_* environment, as the dev container and CI provide them.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from aiops.config import Settings
from aiops.main import create_app

METRICS_TOKEN = "test-metrics-token-0123456789"


def offline_settings(**overrides: Any) -> Settings:
    """Settings whose dependencies are unreachable (closed local ports, short timeouts)."""
    values: dict[str, Any] = {
        "environment": "test",
        "db_host": "127.0.0.1",
        "db_port": 9,
        "db_password": "unused",
        "db_password_file": None,
        "db_connect_timeout_seconds": 0.5,
        "redis_host": "127.0.0.1",
        "redis_port": 9,
        "redis_password": None,
        "redis_password_file": None,
        "redis_timeout_seconds": 0.5,
        "metrics_token": METRICS_TOKEN,
        "metrics_token_file": None,
        "readiness_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return Settings(**values)


def live_settings() -> Settings:
    """Settings for the PostgreSQL and Redis the environment points at."""
    return Settings(
        environment="test",
        metrics_token=SecretStr(METRICS_TOKEN),
        metrics_token_file=None,
        readiness_timeout_seconds=5.0,
    )


@asynccontextmanager
async def serve(
    settings: Settings, customize: Callable[[FastAPI], None] | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app with its lifespan and return a client bound to it."""
    app = create_app(settings)
    if customize is not None:
        customize(app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        yield client


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with serve(offline_settings()) as test_client:
        yield test_client


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.environ.get("AIOPS_TEST_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="needs PostgreSQL and Redis: set AIOPS_TEST_INTEGRATION=1")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
