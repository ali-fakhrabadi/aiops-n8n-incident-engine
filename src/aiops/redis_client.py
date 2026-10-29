"""Redis connections: the API publishes nothing yet, the worker reads the stream."""

from __future__ import annotations

from redis.asyncio import Redis

from aiops.config import RuntimeSettings


def create_redis_client(settings: RuntimeSettings, *, socket_timeout: float | None = None) -> Redis:
    """Redis client; `socket_timeout` must exceed any blocking read (XREADGROUP BLOCK)."""
    password = settings.redis_password.get_secret_value() if settings.redis_password else None
    return Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        db=settings.redis_db,
        password=password,
        socket_timeout=socket_timeout or settings.redis_timeout_seconds,
        socket_connect_timeout=settings.redis_timeout_seconds,
        health_check_interval=30,
        decode_responses=True,
    )
