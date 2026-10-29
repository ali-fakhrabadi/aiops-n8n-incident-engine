"""Prometheus metrics of the worker."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Histogram


class WorkerMetrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.messages = Counter(
            "aiops_worker_messages_total",
            "Stream messages handled, by topic and outcome "
            "(processed, failed, dropped, ignored, dead_lettered).",
            ["topic", "outcome"],
            registry=registry,
        )
        self.duration = Histogram(
            "aiops_worker_handle_seconds",
            "Time spent handling one message, by topic.",
            ["topic"],
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
            registry=registry,
        )
        self.published = Counter(
            "aiops_outbox_published_total",
            "Outbox events published to the Redis stream.",
            registry=registry,
        )
        self.contexts = Counter(
            "aiops_context_collections_total",
            "Log context collections by result (collected, empty, skipped, failed).",
            ["status"],
            registry=registry,
        )
