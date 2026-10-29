"""Typed access to the resources the application factory and lifespan create."""

from __future__ import annotations

from typing import cast

from fastapi import Request
from prometheus_client import CollectorRegistry
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from aiops.config import Settings


def get_settings(request: Request) -> Settings:
    return cast("Settings", request.app.state.settings)


def get_registry(request: Request) -> CollectorRegistry:
    return cast("CollectorRegistry", request.app.state.registry)


def get_engine(request: Request) -> AsyncEngine:
    return cast("AsyncEngine", request.app.state.engine)


def get_redis(request: Request) -> Redis:
    return cast("Redis", request.app.state.redis)


def get_migration_heads(request: Request) -> frozenset[str]:
    return cast("frozenset[str]", request.app.state.migration_heads)
