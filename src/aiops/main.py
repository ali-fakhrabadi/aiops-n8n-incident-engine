"""Application factory: `uvicorn --factory aiops.main:create_app`."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from aiops import __version__
from aiops.api import health, metrics
from aiops.config import Settings, get_settings
from aiops.db.engine import create_database_engine
from aiops.db.migrations import expected_heads
from aiops.errors import install_problem_handlers
from aiops.logs import configure_logging
from aiops.metrics import HttpMetrics, create_registry
from aiops.middleware import RequestContextMiddleware
from aiops.redis_client import create_redis_client

logger = logging.getLogger(__name__)

DESCRIPTION = """
Receives Prometheus/Alertmanager alerts, enriches them with Loki logs, asks an LLM
(Ollama, OpenAI or a deterministic mock) for a root-cause analysis with safe diagnostic
commands, and hands the result to n8n for severity-based routing.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    registry = create_registry(__version__)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Connections open lazily: startup does not wait for PostgreSQL or Redis, and
        # /health/ready reports them until they are reachable.
        app.state.engine = create_database_engine(settings)
        app.state.redis = create_redis_client(settings)
        logger.info(
            "service started", extra={"version": __version__, "environment": settings.environment}
        )
        try:
            yield
        finally:
            await app.state.redis.aclose()
            await app.state.engine.dispose()
            logger.info("service stopped")

    app = FastAPI(
        title="AIOps Incident Engine",
        version=__version__,
        description=DESCRIPTION.strip(),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.registry = registry
    app.state.migration_heads = expected_heads()

    install_problem_handlers(app)
    app.include_router(health.router)
    app.include_router(metrics.router)
    app.add_middleware(RequestContextMiddleware, metrics=HttpMetrics(registry))
    return app
