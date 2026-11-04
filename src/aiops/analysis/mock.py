"""Deterministic, rule-based analysis: the same input always gives the same output.

It exists so CI and reviewers can run the whole pipeline without a model, and as a
baseline the LLMs are compared against. It recognises a handful of common failure
signatures in the log lines, falls back on the alert name, and only ever quotes lines
that are really there.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from aiops.analysis.prompt import AnalysisRequest, PromptLine
from aiops.analysis.schema import Confidence, Evidence, LlmAnalysis, SuggestedCommand


@dataclass(frozen=True)
class Signature:
    category: str
    pattern: re.Pattern[str]
    cause: str
    steps: tuple[str, ...]


SIGNATURES = (
    Signature(
        "memory",
        re.compile(
            r"(?i)out of memory|oom[- ]?kill|cannot allocate memory|memory limit|killed process"
        ),
        "The workload ran out of memory and was killed or failed to allocate.",
        (
            "Compare memory use with the container's limit.",
            "Look for a leak or a load spike before the alert.",
        ),
    ),
    Signature(
        "disk",
        re.compile(r"(?i)no space left on device|disk (is )?full|quota exceeded"),
        "A filesystem is full, so writes fail.",
        (
            "Find what filled the filesystem.",
            "Free space or grow the volume, then confirm writes succeed.",
        ),
    ),
    Signature(
        "dependency",
        re.compile(
            r"(?i)connection refused|could not connect|connection reset|no route to host|"
            r"name or service not known|temporary failure in name resolution|timed? ?out"
        ),
        "A dependency the workload calls is unreachable or too slow.",
        (
            "Check that the dependency is running and reachable from this workload.",
            "Check recent network or DNS changes.",
        ),
    ),
    Signature(
        "crash",
        re.compile(r"(?i)\b(panic|traceback|uncaught exception|segmentation fault|fatal error)\b"),
        "The process crashes on an unhandled error.",
        (
            "Read the stack trace around the first crash.",
            "Check what changed in the last deployment.",
        ),
    ),
    Signature(
        "access",
        re.compile(
            r"(?i)permission denied|unauthori[sz]ed|forbidden|authentication failed|access denied"
        ),
        "The workload is denied access to something it needs (credentials or permissions).",
        ("Check recently rotated credentials and file permissions.",),
    ),
)

_BY_ALERT = (
    (re.compile(r"(?i)memory"), "Memory use is close to or over the limit."),
    (re.compile(r"(?i)disk|filesystem|space"), "A filesystem is running out of space."),
    (re.compile(r"(?i)cpu|throttl"), "The workload uses more CPU than it is allowed."),
    (re.compile(r"(?i)restart|crash"), "The container keeps restarting."),
    (re.compile(r"(?i)down|unreachable|absent"), "A scrape target or service stopped answering."),
)


def _target_commands(labels: Mapping[str, str]) -> list[SuggestedCommand]:
    container = labels.get("name") or labels.get("container")
    project, service = labels.get("compose_project"), labels.get("compose_service")
    if container:
        return [
            SuggestedCommand(
                command=f"docker ps --all --filter name={container}",
                purpose="State and restart count",
            ),
            SuggestedCommand(
                command=f"docker logs --tail 200 {container}", purpose="The latest log lines"
            ),
            SuggestedCommand(
                command=f"docker inspect {container}", purpose="Exit code, OOM flag, limits"
            ),
            SuggestedCommand(
                command=f"docker stats --no-stream {container}", purpose="Current CPU and memory"
            ),
        ]
    if project and service:
        return [
            SuggestedCommand(
                command=f"docker compose -p {project} ps {service}", purpose="Service state"
            ),
            SuggestedCommand(
                command=f"docker compose -p {project} logs --tail 200 {service}",
                purpose="The latest log lines",
            ),
        ]
    return [
        SuggestedCommand(command="uptime", purpose="Load and uptime of the host"),
        SuggestedCommand(command="df -h", purpose="Filesystem usage"),
        SuggestedCommand(command="free -h", purpose="Memory usage"),
    ]


def _matching(
    lines: tuple[PromptLine, ...] | list[PromptLine], pattern: re.Pattern[str]
) -> list[PromptLine]:
    return [line for line in lines if pattern.search(line.text)]


def mock_analysis(request: AnalysisRequest) -> LlmAnalysis:
    lines = list(request.logs.lines)
    commands = _target_commands(request.alert.labels)
    for signature in SIGNATURES:
        hits = _matching(lines, signature.pattern)
        if hits:
            evidence = [
                Evidence(
                    quote=line.text[:400],
                    why=f"A {signature.category} failure signature in the logs",
                )
                for line in hits[:3]
            ]
            confidence = Confidence.HIGH if len(hits) >= 3 else Confidence.MEDIUM  # noqa: PLR2004
            return LlmAnalysis(
                summary=f"{request.alert.alertname}: {signature.cause}",
                probable_cause=signature.cause,
                confidence=confidence,
                evidence=evidence,
                suggested_commands=commands,
                next_steps=list(signature.steps),
            )

    by_alert = next(
        (text for pattern, text in _BY_ALERT if pattern.search(request.alert.alertname)), None
    )
    cause = by_alert or "The alert fired, but nothing in the available evidence explains why."
    reason = (
        "No log line matched a known failure signature."
        if lines
        else (f"No log lines were available ({request.logs.no_lines_reason}).")
    )
    return LlmAnalysis(
        summary=f"{request.alert.alertname}: {cause}",
        probable_cause=f"{cause} {reason}",
        confidence=Confidence.LOW,
        evidence=[],
        suggested_commands=commands,
        next_steps=[
            "Read the full logs of the affected workload.",
            "Check recent changes on the host.",
        ],
    )
