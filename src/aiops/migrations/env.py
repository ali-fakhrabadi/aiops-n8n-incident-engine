"""Alembic environment: async migrations on asyncpg, connection from AIOPS_DB_* settings.

Migrations run as the schema-owner role (the one-shot `migrate` service); the API
connects as a role that can read and write data but not change the schema.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import create_async_engine

from aiops.config import DatabaseSettings
from aiops.db.base import Base
from aiops.db.engine import database_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Render SQL without a connection (`alembic upgrade head --sql`)."""
    context.configure(
        url="postgresql+asyncpg://",
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    settings = DatabaseSettings()
    engine = create_async_engine(
        database_url(settings),
        poolclass=pool.NullPool,
        connect_args={
            "timeout": settings.db_connect_timeout_seconds,
            # Fail fast instead of queueing behind live traffic for a table lock; DDL
            # itself may take longer than the API's statement timeout.
            "server_settings": {
                "application_name": "aiops-migrate",
                "lock_timeout": "5s",
                "statement_timeout": "0",
            },
        },
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
