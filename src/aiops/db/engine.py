"""Async SQLAlchemy engine on asyncpg."""

from __future__ import annotations

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from aiops.config import DatabaseSettings


def database_url(settings: DatabaseSettings) -> URL:
    return URL.create(
        drivername="postgresql+asyncpg",
        username=settings.db_user,
        password=settings.db_password.get_secret_value(),
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
    )


def create_database_engine(settings: DatabaseSettings) -> AsyncEngine:
    return create_async_engine(
        database_url(settings),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={
            "timeout": settings.db_connect_timeout_seconds,
            # A runaway query cannot hold a pooled connection for long.
            "server_settings": {
                "application_name": settings.db_application_name,
                "statement_timeout": str(settings.db_statement_timeout_ms),
            },
        },
    )
