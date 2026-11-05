"""What the worker does with each stream message.

Handlers must be idempotent: the stream delivers at least once (a crash between doing
the work and acknowledging it means the message comes again). State changes are
therefore conditional on the state they expect.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aiops.analysis.providers import Analyzer, AnalyzerError
from aiops.analysis.service import build_request, finalize
from aiops.config import WorkerSettings
from aiops.incidents.models import (
    AnalysisStatus,
    ContextStatus,
    EventKind,
    Incident,
    IncidentEvent,
    OutboxMessage,
)
from aiops.incidents.topics import TOPIC_ANALYZED, TOPIC_CONTEXT_READY, TOPIC_OPENED
from aiops.logcontext.loki import LokiClient, LokiQueryError
from aiops.logcontext.redaction import redact
from aiops.logcontext.selectors import selector_for
from aiops.worker.metrics import WorkerMetrics

logger = logging.getLogger(__name__)


class PermanentError(Exception):
    """The message can never succeed (malformed, refers to nothing): drop it, do not retry."""


class Handler(Protocol):
    topic: str

    async def handle(self, payload: Mapping[str, Any]) -> None: ...

    async def on_exhausted(self, payload: Mapping[str, Any], reason: str) -> None:
        """Called once when retries run out, before the message is dead-lettered."""


def incident_id_of(payload: Mapping[str, Any]) -> uuid.UUID:
    try:
        return uuid.UUID(str(payload["incident_id"]))
    except (KeyError, ValueError) as exc:
        msg = "the message has no valid incident_id"
        raise PermanentError(msg) from exc


class CollectLogContext:
    """incident.opened -> the incident's log lines around its start, redacted."""

    topic = TOPIC_OPENED

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        loki: LokiClient | None,
        settings: WorkerSettings,
        metrics: WorkerMetrics,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._loki = loki
        self._settings = settings
        self._metrics = metrics
        self._clock = clock

    async def handle(self, payload: Mapping[str, Any]) -> None:
        incident_id = incident_id_of(payload)
        async with self._sessions() as session:
            incident = await session.get(Incident, incident_id)
        if incident is None:
            msg = f"incident {incident_id} does not exist"
            raise PermanentError(msg)
        if incident.context_status != ContextStatus.PENDING:
            return  # a redelivery of work already done

        if self._loki is None:
            await self._store(
                incident_id, ContextStatus.SKIPPED, {"reason": "Loki is not configured"}
            )
            return
        selector = selector_for(incident.labels)
        if selector is None:
            await self._store(
                incident_id,
                ContextStatus.SKIPPED,
                {"reason": "no log stream matches the alert's labels"},
            )
            return

        settings = self._settings
        start = incident.starts_at - timedelta(minutes=settings.loki_lookback_minutes)
        end = min(
            self._clock(), incident.starts_at + timedelta(minutes=settings.loki_lookahead_minutes)
        )
        try:
            # LokiUnavailableError propagates: the message is retried later.
            lines = await self._loki.query_range(
                selector.query, start, end, settings.loki_max_lines
            )
        except LokiQueryError as exc:
            await self._store(
                incident_id,
                ContextStatus.FAILED,
                {"reason": str(exc), "selector": selector.query},
            )
            return
        context = {
            "source": "loki",
            "selector": selector.query,
            "rule": selector.rule,
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "limit_reached": len(lines) >= settings.loki_max_lines,
            "lines": [
                {
                    "ts": line.timestamp.isoformat(),
                    "labels": line.labels,
                    "line": redact(line.line)[: settings.loki_max_line_length],
                }
                for line in lines
            ],
        }
        status = ContextStatus.COLLECTED if lines else ContextStatus.EMPTY
        await self._store(incident_id, status, context)

    async def on_exhausted(self, payload: Mapping[str, Any], reason: str) -> None:
        await self._store(incident_id_of(payload), ContextStatus.FAILED, {"reason": reason})

    async def _store(
        self, incident_id: uuid.UUID, status: ContextStatus, context: dict[str, Any]
    ) -> None:
        now = self._clock()
        async with self._sessions() as session, session.begin():
            severity = await session.scalar(
                update(Incident)
                .where(Incident.id == incident_id, Incident.context_status == ContextStatus.PENDING)
                .values(context=context, context_status=status, context_updated_at=now)
                .returning(Incident.severity)
            )
            if severity is None:
                return  # another delivery got there first
            details: dict[str, Any] = {"status": status.value}
            if "lines" in context:
                details["lines"] = len(context["lines"])
                details["selector"] = context["selector"]
            if "reason" in context:
                details["reason"] = context["reason"]
            kind = (
                EventKind.CONTEXT_FAILED
                if status == ContextStatus.FAILED
                else EventKind.CONTEXT_COLLECTED
            )
            session.add(IncidentEvent(incident_id=incident_id, kind=kind, details=details))
            # Next stage, in the same transaction: analysis runs with whatever was found,
            # including nothing (the alert alone is still worth a look). The severity picks
            # the analysis lane: critical alerts never queue behind warnings.
            session.add(
                OutboxMessage(
                    topic=TOPIC_CONTEXT_READY,
                    payload={"incident_id": str(incident_id), "severity": severity},
                )
            )
        self._metrics.contexts.labels(status.value).inc()
        logger.info(
            "log context stored",
            extra={"incident_id": str(incident_id), **details},
        )


class AnalyzeIncident:
    """incident.context_ready -> a root-cause analysis, checked before it is stored."""

    topic = TOPIC_CONTEXT_READY

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        analyzer: Analyzer,
        settings: WorkerSettings,
        metrics: WorkerMetrics,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._analyzer = analyzer
        self._settings = settings
        self._metrics = metrics
        self._clock = clock

    async def handle(self, payload: Mapping[str, Any]) -> None:
        incident_id = incident_id_of(payload)
        async with self._sessions() as session:
            incident = await session.get(Incident, incident_id)
        if incident is None:
            msg = f"incident {incident_id} does not exist"
            raise PermanentError(msg)
        if incident.analysis_status != AnalysisStatus.PENDING:
            return  # a redelivery of work already done

        request = build_request(
            incident,
            max_lines=self._settings.llm_max_log_lines,
            max_chars=self._settings.llm_max_log_chars,
        )
        provider = self._analyzer.provider
        started = time.perf_counter()
        try:
            # AnalyzerUnavailableError propagates: the message is retried later.
            result = await self._analyzer.analyze(request)
        except AnalyzerError as exc:
            await self._store(
                incident_id,
                AnalysisStatus.FAILED,
                {"reason": str(exc), "provider": provider, "model": self._analyzer.model},
            )
            return
        finally:
            self._metrics.analysis_seconds.labels(provider).observe(time.perf_counter() - started)
        duration_ms = round((time.perf_counter() - started) * 1000)
        await self._store(
            incident_id, AnalysisStatus.COMPLETED, finalize(result, request, duration_ms)
        )

    async def on_exhausted(self, payload: Mapping[str, Any], reason: str) -> None:
        await self._store(
            incident_id_of(payload),
            AnalysisStatus.FAILED,
            {"reason": reason, "provider": self._analyzer.provider, "model": self._analyzer.model},
        )

    async def _store(
        self, incident_id: uuid.UUID, status: AnalysisStatus, analysis: dict[str, Any]
    ) -> None:
        async with self._sessions() as session, session.begin():
            changed = await session.scalar(
                update(Incident)
                .where(
                    Incident.id == incident_id, Incident.analysis_status == AnalysisStatus.PENDING
                )
                .values(
                    analysis=analysis, analysis_status=status, analysis_updated_at=self._clock()
                )
                .returning(Incident.id)
            )
            if changed is None:
                return  # another delivery got there first
            details: dict[str, Any] = {
                key: analysis[key]
                for key in ("provider", "model", "duration_ms", "confidence", "reason")
                if key in analysis
            }
            if status == AnalysisStatus.COMPLETED:
                details["commands"] = len(analysis["suggested_commands"])
                details["commands_rejected"] = len(analysis["rejected_commands"])
                details["evidence_dropped"] = analysis["evidence_dropped"]
            kind = (
                EventKind.ANALYSIS_COMPLETED
                if status == AnalysisStatus.COMPLETED
                else EventKind.ANALYSIS_FAILED
            )
            session.add(IncidentEvent(incident_id=incident_id, kind=kind, details=details))
            # Next stage, in the same transaction: a failed analysis is still worth a
            # message (the alert itself, without a probable cause).
            session.add(
                OutboxMessage(topic=TOPIC_ANALYZED, payload={"incident_id": str(incident_id)})
            )
        provider = str(analysis.get("provider", "unknown"))
        self._metrics.analyses.labels(provider, status.value).inc()
        if status == AnalysisStatus.COMPLETED:
            self._metrics.commands_rejected.labels(provider).inc(len(analysis["rejected_commands"]))
            self._metrics.evidence_dropped.labels(provider).inc(analysis["evidence_dropped"])
        logger.info("analysis stored", extra={"incident_id": str(incident_id), **details})
