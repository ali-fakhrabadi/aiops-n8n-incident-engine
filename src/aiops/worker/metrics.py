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
        self.analyses = Counter(
            "aiops_analyses_total",
            "Root-cause analyses by provider and result (completed, failed).",
            ["provider", "status"],
            registry=registry,
        )
        self.analysis_seconds = Histogram(
            "aiops_analysis_seconds",
            "Time the analyser took per incident, by provider.",
            ["provider"],
            buckets=(0.05, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0),
            registry=registry,
        )
        self.commands_rejected = Counter(
            "aiops_analysis_commands_rejected_total",
            "Suggested commands the read-only allowlist rejected, by provider.",
            ["provider"],
            registry=registry,
        )
        self.evidence_dropped = Counter(
            "aiops_analysis_evidence_dropped_total",
            "Evidence quotes dropped because they were not in the logs or the alert, by provider.",
            ["provider"],
            registry=registry,
        )
