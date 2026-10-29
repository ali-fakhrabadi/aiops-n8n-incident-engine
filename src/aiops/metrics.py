"""Prometheus metrics of the API process.

Each application instance owns its registry, so tests can build many apps in one
process. The API runs one Uvicorn worker per container (scale with replicas), which
keeps the default single-process client correct.
"""

from __future__ import annotations

import platform

from prometheus_client import (
    CollectorRegistry,
    Counter,
    GCCollector,
    Histogram,
    Info,
    PlatformCollector,
    ProcessCollector,
)

# Route label for requests that matched no route: raw paths would explode cardinality.
UNMATCHED_ROUTE = "unmatched"

_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def create_registry(version: str) -> CollectorRegistry:
    registry = CollectorRegistry(auto_describe=True)
    ProcessCollector(registry=registry)
    PlatformCollector(registry=registry)
    GCCollector(registry=registry)
    Info("aiops_build", "Version of the running service.", registry=registry).info(
        {"version": version, "python": platform.python_version()}
    )
    return registry


class HttpMetrics:
    """RED metrics per route template (never per raw path)."""

    def __init__(self, registry: CollectorRegistry) -> None:
        self._requests = Counter(
            "aiops_http_requests_total",
            "HTTP requests by method, route template and status code.",
            ["method", "route", "status"],
            registry=registry,
        )
        self._duration = Histogram(
            "aiops_http_request_duration_seconds",
            "HTTP request duration by method and route template.",
            ["method", "route"],
            buckets=_DURATION_BUCKETS,
            registry=registry,
        )

    def observe(self, method: str, route: str, status: int, seconds: float) -> None:
        self._requests.labels(method, route, str(status)).inc()
        self._duration.labels(method, route).observe(seconds)
