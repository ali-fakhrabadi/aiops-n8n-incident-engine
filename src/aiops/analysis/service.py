"""From an incident to a stored analysis: build the request, check the answer.

The checks here do not trust the model. Whatever the prompt said and whatever the logs
tried to make the model do:

* every suggested command passes the read-only allowlist or is moved to
  `rejected_commands` with the reason;
* every evidence quote must appear in the log lines or the alert as given to the model,
  or it is dropped, so the analysis cannot cite logs that do not exist.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from aiops.analysis.prompt import (
    AlertFacts,
    AnalysisRequest,
    LogExcerpt,
    build_messages,
    select_lines,
)
from aiops.analysis.providers import AnalyzerResult
from aiops.analysis.safety import rejection_reason
from aiops.incidents.models import Incident

_SPACE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _SPACE.sub(" ", text).strip().casefold()


def build_request(incident: Incident, *, max_lines: int, max_chars: int) -> AnalysisRequest:
    context: Mapping[str, Any] = incident.context or {}
    lines, omitted = select_lines(
        context.get("lines") or [], max_lines=max_lines, max_chars=max_chars
    )
    reason = (
        None if lines else str(context.get("reason") or f"log context {incident.context_status}")
    )
    alert = AlertFacts(
        incident_id=incident.id,
        alertname=incident.alertname,
        severity=incident.severity,
        status=incident.status,
        starts_at=incident.starts_at,
        flapping=incident.flapping,
        labels=dict(incident.labels),
        annotations=dict(incident.annotations),
    )
    logs = LogExcerpt(lines=lines, omitted=omitted, no_lines_reason=reason)
    return AnalysisRequest(alert=alert, logs=logs, messages=build_messages(alert, logs))


def _sources(request: AnalysisRequest) -> list[str]:
    texts: Iterable[str] = (
        # The rendered line contains the raw text, so either form of quote is found.
        *(line.rendered for line in request.logs.lines),
        *request.alert.labels.values(),
        *request.alert.annotations.values(),
        request.alert.alertname,
    )
    return [_normalise(text) for text in texts]


def finalize(result: AnalyzerResult, request: AnalysisRequest, duration_ms: int) -> dict[str, Any]:
    """The analysis as stored: checked commands, grounded evidence, provenance."""
    analysis = result.analysis
    sources = _sources(request)
    evidence: list[dict[str, Any]] = []
    dropped: list[str] = []
    for item in analysis.evidence:
        quote = _normalise(item.quote)
        if quote and any(quote in source for source in sources):
            evidence.append(item.model_dump())
        else:
            dropped.append(item.quote)
    commands, rejected = [], []
    for suggestion in analysis.suggested_commands:
        reason = rejection_reason(suggestion.command)
        if reason is None:
            commands.append(suggestion.model_dump())
        else:
            rejected.append({"command": suggestion.command, "reason": reason})
    return {
        "provider": result.provider,
        "model": result.model,
        "duration_ms": duration_ms,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "summary": analysis.summary,
        "probable_cause": analysis.probable_cause,
        "confidence": analysis.confidence.value,
        "evidence": evidence,
        "evidence_dropped": len(dropped),
        # Kept for review: an invented quote is worth seeing, not only counting.
        "dropped_evidence": dropped,
        "suggested_commands": commands,
        "rejected_commands": rejected,
        "next_steps": list(analysis.next_steps),
        "log_lines_used": len(request.logs.lines),
        "log_lines_omitted": request.logs.omitted,
    }
