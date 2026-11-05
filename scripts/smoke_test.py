#!/usr/bin/env python3
"""End-to-end checks of the running stack (`make smoke`). Standard library only.

Checks what unit tests cannot: the real containers, their wiring, the published ports
and the database roles. Secrets are read from ./secrets and never printed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ["docker", "compose", "--project-directory", str(ROOT)]


@dataclass(frozen=True)
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> object:
        return json.loads(self.body)


def fetch(
    url: str,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    body: object | None = None,
) -> Reply:
    data = None if body is None else json.dumps(body).encode()
    all_headers = {**(headers or {}), **({"Content-Type": "application/json"} if data else {})}
    request = urllib.request.Request(url, data=data, headers=all_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return Reply(
                response.status,
                {k.lower(): v for k, v in response.headers.items()},
                response.read(),
            )
    except urllib.error.HTTPError as error:
        return Reply(error.code, {k.lower(): v for k, v in error.headers.items()}, error.read())


def psql_as(role: str, secret: str, database: str, sql: str) -> subprocess.CompletedProcess[str]:
    """Run SQL in the postgres container over TCP as `role` (password via PGPASSWORD)."""
    script = (
        f'PGPASSWORD="$(cat /run/secrets/{secret})" '
        f'psql --no-psqlrc -h 127.0.0.1 -U {role} -d {database} -tAc "{sql}"'
    )
    return subprocess.run(
        [*COMPOSE, "exec", "-T", "postgres", "sh", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )


FINAL = {"delivered", "suppressed", "failed"}
# Two injected Telegram failures, each followed by the Telegram node's 5 s wait.
RETRY_WAITS = 10


class Smoke:
    def __init__(self, api: str, n8n: str, secrets: Path, pipeline_timeout: float) -> None:
        self.api = api.rstrip("/")
        self.n8n = n8n.rstrip("/")
        self.pipeline_timeout = pipeline_timeout

        def secret(name: str) -> str:
            return (secrets / name).read_text(encoding="utf-8").strip()

        self.metrics_token = secret("metrics_token")
        self.ingest = {"Authorization": f"Bearer {secret('ingest_token')}"}
        self.api_auth = {"Authorization": f"Bearer {secret('api_token')}"}

    def api_liveness(self) -> str | None:
        reply = fetch(f"{self.api}/health/live")
        return None if reply.status == HTTPStatus.OK else f"HTTP {reply.status}"

    def api_readiness(self) -> str | None:
        reply = fetch(f"{self.api}/health/ready")
        expected = {"status": "ok", "checks": {"database": "ok", "migrations": "ok"}}
        return (
            None
            if reply.status == HTTPStatus.OK and reply.json() == expected
            else f"HTTP {reply.status}: {reply.body!r}"
        )

    def problem_details(self) -> str | None:
        reply = fetch(f"{self.api}/no-such-route", headers={"X-Request-ID": "smoke-test-0001"})
        if (
            reply.status != HTTPStatus.NOT_FOUND
            or reply.headers.get("content-type") != "application/problem+json"
        ):
            return f"HTTP {reply.status}, {reply.headers.get('content-type')}"
        body = reply.json()
        if not isinstance(body, dict) or body.get("request_id") != "smoke-test-0001":
            return f"request ID not propagated: {body}"
        return None

    def metrics_protected(self) -> str | None:
        anonymous = fetch(f"{self.api}/metrics")
        if anonymous.status != HTTPStatus.UNAUTHORIZED:
            return f"without a token: HTTP {anonymous.status}"
        scraped = fetch(
            f"{self.api}/metrics", headers={"Authorization": f"Bearer {self.metrics_token}"}
        )
        if scraped.status != HTTPStatus.OK or b"aiops_http_requests_total" not in scraped.body:
            return f"with the token: HTTP {scraped.status}"
        return None

    def tokens_are_separate(self) -> str | None:
        alerts = f"{self.api}/api/v1/alerts/alertmanager"
        if (status := fetch(alerts, method="POST", body={}).status) != HTTPStatus.UNAUTHORIZED:
            return f"alerts without a token: HTTP {status}"
        incidents = f"{self.api}/api/v1/incidents"
        if (status := fetch(incidents, headers=self.ingest).status) != HTTPStatus.UNAUTHORIZED:
            return f"incidents with the ingest token: HTTP {status}"
        report = f"{self.api}/api/v1/notifications/{uuid.uuid4()}/delivery"
        body = {"status": "processing"}
        if (status := fetch(report, self.api_auth, "POST", body).status) != HTTPStatus.UNAUTHORIZED:
            return f"delivery report with the API token: HTTP {status}"
        return None

    def _incident_through_n8n(self, severity: str) -> tuple[dict[str, Any] | None, str]:
        """Deliver an alert like Alertmanager does and wait until n8n reports back."""
        alertname = f"SmokeTest{severity.capitalize()}"
        alert = {
            "status": "firing",
            "labels": {"alertname": alertname, "severity": severity, "smoke_test": "true"},
            "annotations": {"summary": "Delivered by scripts/smoke_test.py"},
            "startsAt": datetime.now(UTC).isoformat(),
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "",
            "fingerprint": uuid.uuid4().hex[:16],
        }
        delivery = {
            "version": "4",
            "groupKey": f'{{}}:{{alertname="{alertname}"}}',
            "status": "firing",
            "receiver": "smoke-test",
            "alerts": [alert],
        }
        reply = fetch(f"{self.api}/api/v1/alerts/alertmanager", self.ingest, "POST", delivery)
        if (
            reply.status != HTTPStatus.OK
            or not isinstance(body := reply.json(), dict)
            or body.get("opened") != 1
        ):
            return None, f"delivery: HTTP {reply.status}: {reply.body!r}"
        listing = fetch(f"{self.api}/api/v1/incidents?alertname={alertname}&limit=1", self.api_auth)
        page: Any = listing.json() if listing.status == HTTPStatus.OK else {"items": []}
        if not page["items"]:
            return None, f"incident not listed: HTTP {listing.status}"
        incident_url = f"{self.api}/api/v1/incidents/{page['items'][0]['id']}"
        deadline = time.monotonic() + self.pipeline_timeout
        detail: Any = None
        while time.monotonic() < deadline:
            detail = fetch(incident_url, self.api_auth).json()
            if any(item["status"] in FINAL for item in detail["notifications"]):
                return detail, ""
            time.sleep(0.5)
        state = (
            f"analysis {detail['analysis_status']}, notifications {detail['notifications']}"
            if detail
            else "no detail"
        )
        return None, f"no delivery report within {self.pipeline_timeout:.0f} s ({state})"

    @staticmethod
    def _timeline_problem(detail: dict[str, Any], outcome: str) -> str | None:
        kinds = [event["kind"] for event in detail["events"]]
        # The worker records the hand-off and n8n reports the outcome concurrently.
        expected = ["opened", "context_collected", "analysis_completed"]
        if kinds[:3] != expected or sorted(kinds[3:]) != sorted(["notification_sent", outcome]):
            return f"timeline {kinds}"
        return None

    def alert_flows_through_the_pipeline(self) -> str | None:
        """Info alerts go all the way through n8n, which records them without sending."""
        detail, problem = self._incident_through_n8n("info")
        if detail is None:
            return problem
        notification = detail["notifications"][0]
        if (notification["status"], notification["route"]) != ("suppressed", "suppress"):
            return f"notification {notification}"
        return self._timeline_problem(detail, "notification_suppressed")

    def critical_alert_reaches_telegram(self) -> str | None:
        detail, problem = self._incident_through_n8n("critical")
        if detail is None:
            return problem
        notification = detail["notifications"][0]
        if notification["status"] != "delivered" or not notification["channel_message_id"]:
            return f"notification {notification}"
        if notification["route"] != "page":
            return f"routed to {notification['route']}, expected page"
        return self._timeline_problem(detail, "notification_delivered")

    @staticmethod
    def _fake_telegram(path: str, body: object | None = None) -> Any:
        """Call the fake Bot API's test endpoints, from inside its container."""
        script = (
            "import sys, urllib.request as u; data = sys.stdin.read().encode() or None; "
            f"request = u.Request('http://127.0.0.1:8081{path}', data=data, "
            "headers={'Content-Type': 'application/json'}); "
            "print(u.urlopen(request, timeout=10).read().decode())"
        )
        result = subprocess.run(
            [*COMPOSE, "exec", "-T", "fake-telegram", "python", "-c", script],
            input="" if body is None else json.dumps(body),
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

    def fake_telegram_got_a_valid_page(self) -> str | None:
        """The page from the previous check, as the fake Bot API (Telegram's rules) saw it."""
        messages = self._fake_telegram("/_test/messages")["messages"]
        pages = [message for message in messages if "SmokeTestCritical" in message["text"]]
        if not pages:
            return f"no message about SmokeTestCritical among {len(messages)}"
        last = pages[-1]
        if last["parse_mode"] != "HTML" or last["disable_notification"]:
            return f"parse_mode {last['parse_mode']}, silent {last['disable_notification']}"
        return None

    def telegram_failures_are_retried(self) -> str | None:
        """The fake Bot API fails twice (502); n8n's retries still deliver the page."""
        self._fake_telegram("/_test/faults", {"status": 502, "count": 2})
        detail, problem = self._incident_through_n8n("critical")
        if detail is None:
            return problem
        if detail["notifications"][0]["status"] != "delivered":
            return f"notification {detail['notifications'][0]}"
        [delivered] = [e for e in detail["events"] if e["kind"] == "notification_delivered"]
        seconds = delivered["details"]["seconds_in_channel"]
        return None if seconds >= RETRY_WAITS else f"delivered after {seconds} s: no retries?"

    def n8n_ready(self) -> str | None:
        reply = fetch(f"{self.n8n}/healthz/readiness")
        return None if reply.status == HTTPStatus.OK else f"HTTP {reply.status}"

    def n8n_webhook_needs_a_signed_token(self) -> str | None:
        """The workflow is published (404 otherwise) and refuses unsigned requests."""
        url = f"{self.n8n}/webhook/aiops-incident"
        unsigned = fetch(url, method="POST", body={"payload": "{}"})
        forged = fetch(
            url,
            headers={"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.e30.c2lnbmF0dXJl"},
            method="POST",
            body={"payload": "{}"},
        )
        refused = {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}
        if unsigned.status not in refused or forged.status not in refused:
            return f"unsigned: HTTP {unsigned.status}, forged: HTTP {forged.status}"
        return None

    def internal_services_not_published(self) -> str | None:
        published = []
        for service, port in (("postgres", "5432"), ("redis", "6379")):
            result = subprocess.run(
                [*COMPOSE, "port", service, port], capture_output=True, text=True, check=False
            )
            if result.stdout.strip():
                published.append(f"{service} on {result.stdout.strip()}")
        return f"published: {', '.join(published)}" if published else None

    def app_role_cannot_change_schema(self) -> str | None:
        result = psql_as(
            "aiops_app", "aiops_app_db_password", "aiops", "CREATE TABLE smoke_probe (id int)"
        )
        if result.returncode == 0:
            psql_as("aiops_owner", "aiops_owner_db_password", "aiops", "DROP TABLE smoke_probe")
            return "aiops_app could create a table"
        return (
            None
            if "permission denied" in result.stderr
            else f"unexpected error: {result.stderr.strip()}"
        )

    def databases_are_separated(self) -> str | None:
        result = psql_as("n8n", "n8n_db_password", "aiops", "SELECT 1")
        if result.returncode == 0:
            return "the n8n role can connect to the aiops database"
        return (
            None
            if "permission denied" in result.stderr
            else f"unexpected error: {result.stderr.strip()}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--api", default="http://127.0.0.1:8020")
    parser.add_argument("--n8n", default="http://127.0.0.1:5678")
    parser.add_argument("--secrets", type=Path, default=ROOT / "secrets")
    parser.add_argument(
        "--pipeline-timeout",
        type=float,
        default=30,
        help="seconds to wait for n8n's report (a CPU-only LLM needs a few minutes)",
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="also send a critical alert and expect Telegram to accept the message "
        "(on a real chat this sends a real message)",
    )
    parser.add_argument(
        "--fake-telegram",
        action="store_true",
        help="--telegram against docker-compose.fake-telegram.yml, plus its checks: the "
        "message as Telegram would parse it, and retries after injected failures",
    )
    args = parser.parse_args()

    smoke = Smoke(args.api, args.n8n, args.secrets, args.pipeline_timeout)
    checks: list[tuple[str, Callable[[], str | None]]] = [
        ("API liveness", smoke.api_liveness),
        ("API readiness: PostgreSQL and migrations at head", smoke.api_readiness),
        ("Errors are problem+json and carry the request ID", smoke.problem_details),
        ("Metrics require the bearer token", smoke.metrics_protected),
        (
            "Alert delivery, incident API and delivery reports need their own tokens",
            smoke.tokens_are_separate,
        ),
        ("n8n ready (its database reachable)", smoke.n8n_ready),
        (
            "n8n's incident webhook is published and refuses unsigned requests",
            smoke.n8n_webhook_needs_a_signed_token,
        ),
        (
            "An info alert flows API -> worker -> analysis -> n8n, recorded without sending",
            smoke.alert_flows_through_the_pipeline,
        ),
    ]
    if args.telegram or args.fake_telegram:
        checks.append(
            (
                "A critical alert pages on-call through Telegram",
                smoke.critical_alert_reaches_telegram,
            )
        )
    if args.fake_telegram:
        checks += [
            ("Telegram got valid HTML, with sound", smoke.fake_telegram_got_a_valid_page),
            ("Telegram failures are retried by n8n", smoke.telegram_failures_are_retried),
        ]
    checks += [
        (
            "PostgreSQL and Redis are not published on the host",
            smoke.internal_services_not_published,
        ),
        ("The API's database role cannot change the schema", smoke.app_role_cannot_change_schema),
        ("The n8n role cannot open the aiops database", smoke.databases_are_separated),
    ]
    failures = 0
    for name, check in checks:
        try:
            problem = check()
        except (OSError, ValueError) as exc:
            problem = f"{type(exc).__name__}: {exc}"
        if problem is None:
            print(f"PASS  {name}")
        else:
            print(f"FAIL  {name}: {problem}")
            failures += 1
    print(f"\n{len(checks) - failures}/{len(checks)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
