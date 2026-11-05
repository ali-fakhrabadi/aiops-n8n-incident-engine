"""Typed access to the resources the application factory and lifespan create."""

from __future__ import annotations

from typing import cast

from fastapi import Request
from prometheus_client import CollectorRegistry
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from aiops.config import Settings
from aiops.metrics import NotificationMetrics


def get_settings(request: Request) -> Settings:
    return cast("Settings", request.app.state.settings)


def get_registry(request: Request) -> CollectorRegistry:
    return cast("CollectorRegistry", request.app.state.registry)


def get_notification_metrics(request: Request) -> NotificationMetrics:
    return cast("NotificationMetrics", request.app.state.notification_metrics)


def get_engine(request: Request) -> AsyncEngine:
    return cast("AsyncEngine", request.app.state.engine)


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast("async_sessionmaker[AsyncSession]", request.app.state.session_factory)


def get_migration_heads(request: Request) -> frozenset[str]:
    return cast("frozenset[str]", request.app.state.migration_heads)
