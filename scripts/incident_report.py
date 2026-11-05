#!/usr/bin/env python3
"""Measured results of incidents, from the engine's own records: JSON and a Markdown table.

    python3 scripts/incident_report.py --since 2026-11-05T16:00:00+00:00 --project drill \
        --output docs/results/drills-20261105.json

For every incident: when the alert started, when Alertmanager delivered it, the log lines
collected, the analysis (time, confidence, the cause, what the checks removed), and each
message (route, time in n8n and the channel, alert to message). All times come from the
engine's timeline, so they share one clock. Only the labels named by --keep-label are
written out: instance labels (host addresses) and the like stay on the host.
Standard library only; the API token is read from ./secrets and never printed.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


class Engine:
    def __init__(self, api: str, token_file: Path) -> None:
        self.api = api.rstrip("/")
        self.token = token_file.read_text(encoding="utf-8").strip()

    def get(self, path: str) -> Any:
        request = urllib.request.Request(
            f"{self.api}{path}", headers={"Authorization": f"Bearer {self.token}"}
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)

    def incidents(self, since: datetime) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        cursor = ""
        while True:
            page = self.get(f"/api/v1/incidents?limit=200{cursor}")
            for item in page["items"]:
                if datetime.fromisoformat(item["first_received_at"]) < since:
                    return found
                found.append(self.get(f"/api/v1/incidents/{item['id']}"))
            if not page["next_cursor"]:
                return found
            cursor = f"&cursor={page['next_cursor']}"


def _seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 1)


def _event(detail: dict[str, Any], kind: str, **match: Any) -> dict[str, Any] | None:
    for event in detail["events"]:
        if event["kind"] == kind and all(event["details"].get(k) == v for k, v in match.items()):
            return event  # type: ignore[no-any-return]
    return None


def summarise(detail: dict[str, Any], keep_labels: set[str]) -> dict[str, Any]:
    analysis = detail.get("analysis") or {}
    context = detail.get("context") or {}
    analysed = _event(detail, "analysis_completed") or _event(detail, "analysis_failed")
    messages = []
    for notification in detail["notifications"]:
        outcome = next(
            (
                event
                for event in detail["events"]
                if event["kind"]
                in {"notification_delivered", "notification_suppressed", "notification_failed"}
                and event["details"].get("notification_id") == notification["id"]
            ),
            None,
        )
        start = detail["resolved_at"] if notification["kind"] == "resolved" else detail["starts_at"]
        messages.append(
            {
                "kind": notification["kind"],
                "status": notification["status"],
                "route": notification["route"],
                "seconds_in_channel": outcome["details"].get("seconds_in_channel")
                if outcome
                else None,
                "alert_to_message_seconds": _seconds(start, notification["completed_at"]),
                "analysis_to_handoff_seconds": _seconds(
                    analysed["occurred_at"] if analysed else None, notification["handed_off_at"]
                )
                if notification["kind"] == "firing"
                else None,
            }
        )
    return {
        "alertname": detail["alertname"],
        "severity": detail["severity"],
        "labels": {k: v for k, v in detail["labels"].items() if k in keep_labels},
        "started_at": detail["starts_at"],
        "alertmanager_seconds": _seconds(detail["starts_at"], detail["first_received_at"]),
        "duration_seconds": _seconds(detail["starts_at"], detail["resolved_at"]),
        "log_lines": len(context.get("lines", [])),
        "context_status": detail["context_status"],
        "analysis_status": detail["analysis_status"],
        "model": f"{analysis.get('provider')} {analysis.get('model')}" if analysis else None,
        "model_seconds": round(analysis["duration_ms"] / 1000, 1)
        if analysis.get("duration_ms")
        else None,
        "confidence": analysis.get("confidence"),
        "probable_cause": analysis.get("probable_cause") or analysis.get("reason"),
        "commands": [c["command"] for c in analysis.get("suggested_commands", [])],
        "commands_rejected": len(analysis.get("rejected_commands", [])),
        "evidence_kept": len(analysis.get("evidence", [])),
        "evidence_dropped": analysis.get("evidence_dropped", 0),
        "messages": messages,
    }


def markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "| Alert | Severity | Alertmanager | Log lines | Page | Model | Confidence "
        "| Checks removed | Route | n8n + Telegram | Alert to analysis |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]

    def value(number: float | None, unit: str = " s") -> str:
        return "-" if number is None else f"{number:g}{unit}"

    for row in rows:
        sent = {message["kind"]: message for message in row["messages"]}
        firing: dict[str, Any] = sent.get("firing", {})
        page: dict[str, Any] = sent.get("page", {})
        removed = f"{row['commands_rejected']} cmd, {row['evidence_dropped']} quote"
        lines.append(
            f"| {row['alertname']} | {row['severity']} | {value(row['alertmanager_seconds'])} "
            f"| {row['log_lines']} | {value(page.get('alert_to_message_seconds'))} "
            f"| {value(row['model_seconds'])} | {row['confidence'] or '-'} "
            f"| {removed} | {firing.get('route') or '-'} "
            f"| {value(firing.get('seconds_in_channel'))} "
            f"| {value(firing.get('alert_to_message_seconds'))} |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--since", required=True, type=datetime.fromisoformat)
    parser.add_argument("--until", type=datetime.fromisoformat, help="only incidents before this")
    parser.add_argument("--project", help="only incidents with this compose_project label")
    parser.add_argument("--alertname", action="append", default=[])
    parser.add_argument(
        "--keep-label",
        action="append",
        default=["alertname", "severity", "compose_project", "compose_service", "name"],
    )
    parser.add_argument("--api", default="http://127.0.0.1:8020")
    parser.add_argument("--token-file", type=Path, default=ROOT / "secrets" / "api_token")
    parser.add_argument("--output", type=Path, help="write the JSON here")
    args = parser.parse_args()

    engine = Engine(args.api, args.token_file)
    rows = []
    for detail in reversed(engine.incidents(args.since)):
        if args.until and datetime.fromisoformat(detail["first_received_at"]) >= args.until:
            continue
        if args.project and detail["labels"].get("compose_project") != args.project:
            continue
        if args.alertname and detail["alertname"] not in args.alertname:
            continue
        rows.append(summarise(detail, set(args.keep_label)))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    print(markdown(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
