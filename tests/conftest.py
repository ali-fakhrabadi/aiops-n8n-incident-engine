"""Shared fixtures.

Unit tests build settings that point at closed ports, so they never need PostgreSQL or
Redis. Integration tests (marked `integration`) take connection settings from the
AIOPS_* environment, as the dev container and CI provide them, migrate that database
once per session and empty its tables before every test.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aiops.config import Settings, WorkerSettings
from aiops.db.engine import create_database_engine
from aiops.main import create_app
from aiops.redis_client import create_redis_client

ROOT = Path(__file__).resolve().parent.parent
METRICS_TOKEN = "test-metrics-token-0123456789"
INGEST_TOKEN = "test-ingest-token-0123456789"
API_TOKEN = "test-api-token-0123456789abcd"
CALLBACK_TOKEN = "test-callback-token-0123456789"
INTEGRATION = os.environ.get("AIOPS_TEST_INTEGRATION") == "1"

TOKENS: dict[str, Any] = {
    "metrics_token": SecretStr(METRICS_TOKEN),
    "metrics_token_file": None,
    "ingest_token": SecretStr(INGEST_TOKEN),
    "ingest_token_file": None,
    "api_token": SecretStr(API_TOKEN),
    "api_token_file": None,
    "callback_token": SecretStr(CALLBACK_TOKEN),
    "callback_token_file": None,
}


def offline_settings(**overrides: Any) -> Settings:
    """Settings whose database is unreachable (closed local port, short timeouts)."""
    values: dict[str, Any] = {
        "environment": "test",
        "db_host": "127.0.0.1",
        "db_port": 9,
        "db_password": SecretStr("unused"),
        "db_password_file": None,
        "db_connect_timeout_seconds": 0.5,
        "redis_password": None,
        "redis_password_file": None,
        "readiness_timeout_seconds": 1.0,
        **TOKENS,
    }
    values.update(overrides)
    return Settings(**values)


def live_settings(**overrides: Any) -> Settings:
    """Settings for the PostgreSQL the environment points at."""
    values: dict[str, Any] = {"environment": "test", "readiness_timeout_seconds": 5.0, **TOKENS}
    values.update(overrides)
    return Settings(**values)


def worker_settings(tmp_path: Path, **overrides: Any) -> WorkerSettings:
    """Worker settings with streams of their own, so tests never share messages."""
    run = uuid.uuid4().hex[:8]
    values: dict[str, Any] = {
        "environment": "test",
        "stream_name": f"test:{run}:incidents",
        "analysis_stream_name": f"test:{run}:analysis",
        "critical_analysis_stream_name": f"test:{run}:analysis:critical",
        "dead_letter_stream_name": f"test:{run}:dead",
        "consumer_group": "test-workers",
        "consumer_name": "test-consumer",
        "read_block_ms": 100,
        "claim_idle_seconds": 0,
        "heartbeat_file": tmp_path / "heartbeat",
    }
    values.update(overrides)
    return WorkerSettings(**values)


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


@pytest.fixture(scope="session")
def migrated() -> None:
    """Bring the test database to the migration head once per session."""
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")


@pytest.fixture
async def sessions(migrated: None) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions on the test database, emptied before the test."""
    engine = create_database_engine(live_settings())
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM incident_events"))
        await connection.execute(text("DELETE FROM notifications"))
        await connection.execute(text("DELETE FROM outbox"))
        await connection.execute(text("DELETE FROM incidents"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def api(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[httpx.AsyncClient]:
    """The API on the (emptied) test database."""
    async with serve(live_settings()) as test_client:
        yield test_client


@pytest.fixture
async def redis(tmp_path: Path) -> AsyncIterator[Redis]:
    client = create_redis_client(worker_settings(tmp_path), socket_timeout=5)
    yield client
    async for key in client.scan_iter(match="test:*"):
        await client.delete(key)
    await client.aclose()


@pytest.fixture
def ingest_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {INGEST_TOKEN}"}


@pytest.fixture
def api_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_TOKEN}"}


@pytest.fixture
def callback_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {CALLBACK_TOKEN}"}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if INTEGRATION:
        return
    skip = pytest.mark.skip(reason="needs PostgreSQL and Redis: set AIOPS_TEST_INTEGRATION=1")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
