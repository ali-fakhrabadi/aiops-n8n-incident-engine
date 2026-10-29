"""Redis connection used by the API (and, from phase 2, the alert queue)."""

from __future__ import annotations

from redis.asyncio import Redis

from aiops.config import Settings


def create_redis_client(settings: Settings) -> Redis:
    password = settings.redis_password.get_secret_value() if settings.redis_password else None
    return Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        db=settings.redis_db,
        password=password,
        socket_timeout=settings.redis_timeout_seconds,
        socket_connect_timeout=settings.redis_timeout_seconds,
        health_check_interval=30,
    )
