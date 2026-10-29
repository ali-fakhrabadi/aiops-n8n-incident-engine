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
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ["docker", "compose", "--project-directory", str(ROOT)]


@dataclass(frozen=True)
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> object:
        return json.loads(self.body)


def fetch(url: str, headers: dict[str, str] | None = None, method: str = "GET") -> Reply:
    request = urllib.request.Request(url, headers=headers or {}, method=method)
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


class Smoke:
    def __init__(self, api: str, n8n: str, secrets: Path) -> None:
        self.api = api.rstrip("/")
        self.n8n = n8n.rstrip("/")
        self.metrics_token = (secrets / "metrics_token").read_text(encoding="utf-8").strip()

    def api_liveness(self) -> str | None:
        reply = fetch(f"{self.api}/health/live")
        return None if reply.status == HTTPStatus.OK else f"HTTP {reply.status}"

    def api_readiness(self) -> str | None:
        reply = fetch(f"{self.api}/health/ready")
        expected = {"status": "ok", "checks": {"database": "ok", "migrations": "ok", "redis": "ok"}}
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

    def n8n_ready(self) -> str | None:
        reply = fetch(f"{self.n8n}/healthz/readiness")
        return None if reply.status == HTTPStatus.OK else f"HTTP {reply.status}"

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
    args = parser.parse_args()

    smoke = Smoke(args.api, args.n8n, args.secrets)
    checks: list[tuple[str, Callable[[], str | None]]] = [
        ("API liveness", smoke.api_liveness),
        ("API readiness: PostgreSQL, Redis, migrations at head", smoke.api_readiness),
        ("Errors are problem+json and carry the request ID", smoke.problem_details),
        ("Metrics require the bearer token", smoke.metrics_protected),
        ("n8n ready (its database reachable)", smoke.n8n_ready),
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
