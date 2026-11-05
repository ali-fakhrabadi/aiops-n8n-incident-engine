#!/usr/bin/env python3
"""Fire an alert at the running engine like Alertmanager does, and follow it.

    python3 scripts/simulate_alert.py --list
    python3 scripts/simulate_alert.py disk-full --severity critical
    python3 scripts/simulate_alert.py out-of-memory --resolve-after 60

The alerts are the ones the analysis evaluation uses (evaluation/scenarios.json). The
incident's timeline is printed as it happens, each stage with its time since the alert
arrived (on the engine's clock): what the engine adds before a message reaches the
chat. Standard library only; tokens are read from ./secrets and never printed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "evaluation" / "scenarios.json"
FINAL = {"delivered", "suppressed", "failed"}


class Engine:
    def __init__(self, api: str, secrets: Path) -> None:
        self.api = api.rstrip("/")

        def bearer(name: str) -> dict[str, str]:
            token = (secrets / name).read_text(encoding="utf-8").strip()
            return {"Authorization": f"Bearer {token}"}

        self.ingest = bearer("ingest_token")
        self.read = bearer("api_token")

    def call(self, path: str, headers: dict[str, str], body: object | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            f"{self.api}{path}",
            data=data,
            headers={**headers, **({"Content-Type": "application/json"} if data else {})},
            method="POST" if data else "GET",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)


def _alert(scenario: dict[str, Any], severity: str | None, run: str) -> dict[str, Any]:
    spec = scenario["alert"]
    return {
        "status": "firing",
        "labels": {
            **spec.get("labels", {}),
            "alertname": spec["alertname"],
            "severity": severity or spec["severity"],
            "simulation": run,
        },
        "annotations": spec.get("annotations", {}),
        "startsAt": datetime.now(UTC).isoformat(),
        "endsAt": "0001-01-01T00:00:00Z",
        "generatorURL": "",
        "fingerprint": run[:16],
    }


def _deliver(engine: Engine, alert: dict[str, Any]) -> None:
    webhook = {
        "version": "4",
        "groupKey": f'{{}}:{{alertname="{alert["labels"]["alertname"]}"}}',
        "status": alert["status"],
        "receiver": "simulate-alert",
        "alerts": [alert],
    }
    engine.call("/api/v1/alerts/alertmanager", engine.ingest, webhook)


def _find(engine: Engine, alertname: str, run: str, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        page = engine.call(f"/api/v1/incidents?alertname={alertname}&limit=20", engine.read)
        for item in page["items"]:
            if item["labels"].get("simulation") == run:
                return str(item["id"])
        time.sleep(0.5)
    msg = "the incident did not appear"
    raise TimeoutError(msg)


def _seconds(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def _describe(event: dict[str, Any]) -> str:
    details = event["details"]
    kind = event["kind"]
    if kind.startswith("context"):
        return details.get("reason") or f"{details.get('lines', 0)} log lines ({details['status']})"
    if kind.startswith("analysis"):
        if "reason" in details:
            return f"{details.get('provider')}: {details['reason']}"
        return (
            f"{details.get('provider')} {details.get('model')}, {details.get('confidence')} "
            f"confidence, {details.get('duration_ms', 0) / 1000:.1f} s"
        )
    if kind == "notification_sent":
        return f"{details['kind']} message handed to n8n ({details['attempts']} attempt(s))"
    if kind.startswith("notification_"):
        parts = [f"{details.get('kind')} message", f"route {details.get('route')}"]
        if details.get("seconds_in_channel") is not None:
            parts.append(f"{details['seconds_in_channel']:.1f} s in n8n and the channel")
        if details.get("error"):
            parts.append(details["error"])
        return ", ".join(parts)
    return ""


def _follow(engine: Engine, incident_id: str, kind: str, timeout: float, seen: int) -> int:
    """Print new timeline events until n8n reports on the `kind` message; returns `seen`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = engine.call(f"/api/v1/incidents/{incident_id}", engine.read)
        for event in detail["events"][seen:]:
            since = _seconds(detail["first_received_at"], event["occurred_at"])
            print(f"  +{since:7.2f} s  {event['kind']:<24} {_describe(event)}")
        seen = len(detail["events"])
        if any(n["kind"] == kind and n["status"] in FINAL for n in detail["notifications"]):
            return seen
        time.sleep(0.5)
    msg = f"no report on the {kind} message within {timeout:.0f} s"
    raise TimeoutError(msg)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("scenario", nargs="?", help="a scenario name (see --list)")
    parser.add_argument("--list", action="store_true", help="list the scenarios")
    parser.add_argument("--severity", choices=["critical", "warning", "info"])
    parser.add_argument(
        "--resolve-after", type=float, metavar="SECONDS", help="then resolve the alert"
    )
    parser.add_argument("--api", default="http://127.0.0.1:8020")
    parser.add_argument("--secrets", type=Path, default=ROOT / "secrets")
    parser.add_argument(
        "--timeout", type=float, default=600, help="seconds to wait for each message's report"
    )
    args = parser.parse_args()

    scenarios = {item["name"]: item for item in json.loads(SCENARIOS.read_text("utf-8"))}
    if args.list or not args.scenario:
        for name, item in scenarios.items():
            print(f"{name:<22} {item['alert']['severity']:<8} {item['description']}")
        return 0
    if args.scenario not in scenarios:
        print(f"unknown scenario {args.scenario!r}; --list shows them")
        return 2

    engine = Engine(args.api, args.secrets)
    run = uuid.uuid4().hex
    alert = _alert(scenarios[args.scenario], args.severity, run)
    labels = alert["labels"]
    try:
        _deliver(engine, alert)
        incident_id = _find(engine, labels["alertname"], run, timeout=30)
        print(f"{labels['alertname']} ({labels['severity']}) fired: incident {incident_id}")
        seen = _follow(engine, incident_id, "firing", args.timeout, 0)
        if args.resolve_after is not None:
            time.sleep(args.resolve_after)
            _deliver(
                engine, {**alert, "status": "resolved", "endsAt": datetime.now(UTC).isoformat()}
            )
            print("resolved")
            _follow(engine, incident_id, "resolved", args.timeout, seen)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
