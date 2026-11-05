"""Worker process: `python -m aiops.worker`.

Runs the outbox relay and the stream consumer side by side until SIGTERM, then finishes
the message in hand and exits. Several replicas may run at once.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket

import httpx
from prometheus_client import start_http_server
from sqlalchemy.ext.asyncio import async_sessionmaker

from aiops import __version__
from aiops.analysis.providers import build_analyzer
from aiops.config import WorkerSettings
from aiops.db.engine import create_database_engine
from aiops.incidents.topics import TOPIC_ANALYZED, TOPIC_RESOLVED
from aiops.logcontext.loki import LokiClient
from aiops.logs import configure_logging
from aiops.metrics import create_registry
from aiops.notifications.client import N8nClient, RetryPolicy
from aiops.notifications.signing import Signer
from aiops.redis_client import create_redis_client
from aiops.worker.consumer import StreamConsumer
from aiops.worker.handlers import AnalyzeIncident, CollectLogContext, Handler
from aiops.worker.metrics import WorkerMetrics
from aiops.worker.notify import NotifyIncident
from aiops.worker.relay import OutboxRelay

logger = logging.getLogger("aiops.worker")


async def run(settings: WorkerSettings, stop: asyncio.Event | None = None) -> None:
    """Run until `stop` is set; without one, until SIGTERM or SIGINT."""
    configure_logging(settings.log_level)
    registry = create_registry(__version__)
    metrics = WorkerMetrics(registry)
    # Not published on the host: Prometheus reaches it on the internal network.
    start_http_server(settings.metrics_port, registry=registry)

    engine = create_database_engine(settings)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    # Reads block for read_block_ms, so the socket timeout must be longer than that.
    redis = create_redis_client(settings, socket_timeout=settings.read_block_ms / 1000 + 5)
    http = httpx.AsyncClient(timeout=settings.loki_timeout_seconds)
    loki = LokiClient(settings.loki_url, http) if settings.loki_url else None
    # Its own client: a CPU-bound model needs a far longer timeout than Loki.
    llm_http = httpx.AsyncClient(timeout=settings.llm_timeout_seconds)
    analyzer = build_analyzer(settings, llm_http)

    n8n_http = httpx.AsyncClient(timeout=settings.n8n_timeout_seconds)
    n8n = (
        N8nClient(
            settings.n8n_webhook_url,
            Signer(
                settings.n8n_webhook_secret, ttl_seconds=settings.notification_token_ttl_seconds
            ),
            n8n_http,
            RetryPolicy(settings.n8n_attempts, settings.n8n_backoff_seconds),
        )
        if settings.n8n_webhook_url and settings.n8n_webhook_secret
        else None
    )

    handlers: list[Handler] = [
        CollectLogContext(sessions, loki, settings, metrics),
        AnalyzeIncident(sessions, analyzer, settings, metrics),
        NotifyIncident(TOPIC_ANALYZED, sessions, n8n, metrics),
        NotifyIncident(TOPIC_RESOLVED, sessions, n8n, metrics),
    ]
    consumer_name = settings.consumer_name or f"{socket.gethostname()}-{os.getpid()}"
    relay = OutboxRelay(sessions, redis, settings, metrics)
    consumer = StreamConsumer(
        redis, settings, {handler.topic: handler for handler in handlers}, metrics, consumer_name
    )

    if stop is None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)

    logger.info(
        "worker started",
        extra={
            "version": __version__,
            "consumer": consumer_name,
            "stream": settings.stream_name,
            "loki": bool(loki),
            "analyzer": f"{analyzer.provider}:{analyzer.model}",
            "notifications": bool(n8n),
        },
    )
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(relay.run(stop))
            tasks.create_task(consumer.run(stop))
    finally:
        await http.aclose()
        await llm_http.aclose()
        await n8n_http.aclose()
        await redis.aclose()
        await engine.dispose()
        logger.info("worker stopped")


def main() -> None:
    asyncio.run(run(WorkerSettings()))


if __name__ == "__main__":
    main()
