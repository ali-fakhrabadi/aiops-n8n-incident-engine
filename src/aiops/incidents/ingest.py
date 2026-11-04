"""Turn Alertmanager deliveries into incidents, idempotently.

Alertmanager delivers at least once: it retries failed requests, re-sends firing alerts
every group interval, and in an HA pair every replica sends its own copy. Every
delivery therefore has to be safe to apply again:

* An insert that loses the race on (fingerprint, starts_at) becomes an update.
* Alerts are processed in a fixed order, so concurrent batches lock rows in the same
  order and cannot deadlock.
* A resolved incident is never reopened: a firing notification for it is a late retry.
  A genuine re-fire has a new start time and opens a new incident.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aiops.incidents.alertmanager import AlertmanagerAlert, AlertmanagerWebhook
from aiops.incidents.models import (
    AnalysisStatus,
    ContextStatus,
    EventKind,
    Incident,
    IncidentEvent,
    IncidentStatus,
    OutboxMessage,
)
from aiops.incidents.topics import TOPIC_OPENED, TOPIC_RESOLVED


class Outcome(StrEnum):
    OPENED = "opened"
    UPDATED = "updated"
    RESOLVED = "resolved"
    IGNORED = "ignored"


@dataclass(frozen=True)
class IngestPolicy:
    flap_threshold: int
    flap_window: timedelta
    ignored_alertnames: frozenset[str] = frozenset({"Watchdog", "InfoInhibitor"})


@dataclass(frozen=True)
class IngestResult:
    received: int
    opened: int
    updated: int
    resolved: int
    ignored: int
    flapping: int


async def ingest(
    session: AsyncSession, webhook: AlertmanagerWebhook, *, policy: IngestPolicy, now: datetime
) -> IngestResult:
    """Apply one delivery inside the caller's transaction."""
    delivery = _Delivery(session, webhook.receiver, policy, now)
    outcomes: Counter[Outcome] = Counter()
    flapping = 0
    for alert in sorted(webhook.alerts, key=lambda item: (item.identity, item.starts_at)):
        if alert.alertname in policy.ignored_alertnames:
            outcomes[Outcome.IGNORED] += 1
            continue
        outcome, is_flapping = await delivery.apply(alert)
        outcomes[outcome] += 1
        flapping += is_flapping
    return IngestResult(
        received=len(webhook.alerts),
        opened=outcomes[Outcome.OPENED],
        updated=outcomes[Outcome.UPDATED],
        resolved=outcomes[Outcome.RESOLVED],
        ignored=outcomes[Outcome.IGNORED],
        flapping=flapping,
    )


@dataclass(frozen=True)
class _Delivery:
    session: AsyncSession
    receiver: str
    policy: IngestPolicy
    now: datetime

    async def apply(self, alert: AlertmanagerAlert) -> tuple[Outcome, bool]:
        now = self.now
        resolved = alert.status == IncidentStatus.RESOLVED
        inserted = await self.session.scalar(
            insert(Incident)
            .values(
                id=uuid.uuid4(),
                fingerprint=alert.identity,
                starts_at=alert.starts_at,
                ends_at=(alert.ends_at or now) if resolved else None,
                alertname=alert.alertname,
                severity=alert.severity.value,
                status=alert.status.value,
                labels=alert.labels,
                annotations=alert.annotations,
                generator_url=alert.generator_url or None,
                first_received_at=now,
                last_received_at=now,
                resolved_at=now if resolved else None,
                notification_count=1,
                flapping=False,
                # An alert that is already resolved when first seen needs no investigation.
                context_status=ContextStatus.SKIPPED if resolved else ContextStatus.PENDING,
                analysis_status=AnalysisStatus.SKIPPED if resolved else AnalysisStatus.PENDING,
            )
            .on_conflict_do_nothing(index_elements=["fingerprint", "starts_at"])
            .returning(Incident.id)
        )
        if inserted is not None:
            return Outcome.OPENED, await self._opened(inserted, alert)

        incident = await self.session.scalar(
            select(Incident)
            .where(Incident.fingerprint == alert.identity, Incident.starts_at == alert.starts_at)
            .with_for_update()
        )
        if incident is None:  # pragma: no cover - the conflicting row cannot vanish under the lock
            msg = "incident disappeared during ingestion"
            raise RuntimeError(msg)
        incident.last_received_at = now
        incident.notification_count += 1
        incident.annotations = alert.annotations
        if incident.status == IncidentStatus.FIRING and resolved:
            incident.status = IncidentStatus.RESOLVED
            incident.ends_at = alert.ends_at or now
            incident.resolved_at = now
            duration = (incident.ends_at - incident.starts_at).total_seconds()
            self.session.add(
                IncidentEvent(
                    incident_id=incident.id,
                    kind=EventKind.RESOLVED,
                    details={"duration_seconds": round(duration, 1)},
                )
            )
            self.session.add(
                OutboxMessage(topic=TOPIC_RESOLVED, payload={"incident_id": str(incident.id)})
            )
            return Outcome.RESOLVED, False
        return Outcome.UPDATED, False

    async def _opened(self, incident_id: uuid.UUID, alert: AlertmanagerAlert) -> bool:
        details: dict[str, object] = {"receiver": self.receiver, "severity": alert.severity.value}
        flapping = False
        if alert.status == IncidentStatus.FIRING:
            # The new row is visible to this count, so the threshold includes it.
            window = self.policy.flap_window
            recent = await self.session.scalar(
                select(func.count())
                .select_from(Incident)
                .where(
                    Incident.fingerprint == alert.identity,
                    Incident.first_received_at >= self.now - window,
                )
            )
            flapping = (recent or 0) >= self.policy.flap_threshold
            if flapping:
                incident = await self.session.get_one(Incident, incident_id)
                incident.flapping = True
                details["flapping"] = {
                    "occurrences": recent,
                    "window_minutes": int(window.total_seconds() // 60),
                }
            self.session.add(
                OutboxMessage(topic=TOPIC_OPENED, payload={"incident_id": str(incident_id)})
            )
        self.session.add(
            IncidentEvent(incident_id=incident_id, kind=EventKind.OPENED, details=details)
        )
        if alert.status == IncidentStatus.RESOLVED:
            self.session.add(
                IncidentEvent(
                    incident_id=incident_id,
                    kind=EventKind.RESOLVED,
                    details={"resolved_on_arrival": True},
                )
            )
        return flapping
