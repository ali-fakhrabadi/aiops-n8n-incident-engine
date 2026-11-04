"""Evaluation scenarios (evaluation/scenarios.json) as incidents an analyser can be run on."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiops.incidents.models import AnalysisStatus, ContextStatus, Incident


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    incident: Incident
    keywords: tuple[str, ...]
    forbidden: tuple[str, ...]


def load_scenarios(path: Path) -> list[Scenario]:
    scenarios = []
    for item in json.loads(path.read_text(encoding="utf-8")):
        lines: list[dict[str, Any]] = item["lines"]
        first = (
            datetime.fromisoformat(lines[0]["ts"]) if lines else datetime(2026, 11, 5, tzinfo=UTC)
        )
        alert = item["alert"]
        labels = {"alertname": alert["alertname"], "severity": alert["severity"], **alert["labels"]}
        context: dict[str, Any] = (
            {
                "source": "loki",
                "lines": [
                    {
                        "ts": line["ts"],
                        "labels": {
                            "level": line["level"],
                            "container": labels.get("name") or labels.get("compose_service", ""),
                        },
                        "line": line["line"],
                    }
                    for line in lines
                ],
            }
            if lines
            else {"reason": item.get("no_lines_reason", "no lines")}
        )
        incident = Incident(
            # Stable IDs: the prompt (and so a seeded model's answer) depends on them.
            id=uuid.uuid5(uuid.NAMESPACE_URL, f"aiops-scenario:{item['name']}"),
            fingerprint="0" * 16,
            starts_at=first,
            alertname=alert["alertname"],
            severity=alert["severity"],
            status="firing",
            labels=labels,
            annotations=alert["annotations"],
            flapping=False,
            context_status=ContextStatus.COLLECTED if lines else ContextStatus.SKIPPED,
            context=context,
            analysis_status=AnalysisStatus.PENDING,
        )
        expect = item["expect"]
        scenarios.append(
            Scenario(
                name=item["name"],
                description=item["description"],
                incident=incident,
                keywords=tuple(expect["keywords"]),
                forbidden=tuple(expect.get("forbidden", ())),
            )
        )
    return scenarios
