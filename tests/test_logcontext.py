from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from aiops.logcontext.loki import LokiClient, LokiQueryError, LokiUnavailableError
from aiops.logcontext.redaction import REDACTED, redact
from aiops.logcontext.selectors import selector_for

LOKI = "http://loki.test:3100"
QUERY_RANGE = f"{LOKI}/loki/api/v1/query_range"
START = datetime(2026, 10, 29, 8, 45, tzinfo=UTC)
END = START + timedelta(minutes=17)


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (
            {"compose_project": "shop", "compose_service": "api", "name": "shop-api-1"},
            '{compose_project="shop", compose_service="api"}',
        ),
        (
            {
                "container_label_com_docker_compose_project": "shop",
                "container_label_com_docker_compose_service": "db",
            },
            '{compose_project="shop", compose_service="db"}',
        ),
        ({"name": "shop-api-1"}, '{container="shop-api-1"}'),
        ({"job": "grafana", "instance": "grafana:3000"}, '{compose_service="grafana"}'),
    ],
)
def test_selector_follows_the_first_matching_rule(labels: dict[str, str], expected: str) -> None:
    selector = selector_for({"alertname": "X", **labels})

    assert selector is not None
    assert selector.query == expected


def test_no_selector_when_no_rule_matches() -> None:
    assert selector_for({"alertname": "HostDiskSpaceLow", "instance": "host"}) is None


def test_label_values_cannot_break_out_of_the_selector() -> None:
    selector = selector_for({"name": 'evil"} |= "x" or {job=~".+'})

    assert selector is not None
    assert selector.query == r'{container="evil\"} |= \"x\" or {job=~\".+"}'


@pytest.mark.parametrize(
    ("line", "leaked"),
    [
        ("connecting to postgres://app:s3cr3tpw@db:5432/app", "s3cr3tpw"),
        ("Authorization: Bearer abc.def-ghi_jkl", "abc.def-ghi_jkl"),
        ('{"db_password": "hunter2", "user": "app"}', "hunter2"),
        ("api_key=AbCdEf123456 retries=3", "AbCdEf123456"),
        ("token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl", "eyJzdWIiOiIxIn0"),
        ("aws key AKIAABCDEFGHIJKLMNOP in env", "AKIAABCDEFGHIJKLMNOP"),
        ("-----BEGIN RSA PRIVATE KEY----- MIIEpAIBAAKCAQEA", "MIIEpAIBAAKCAQEA"),
    ],
)
def test_redaction_removes_credentials(line: str, leaked: str) -> None:
    cleaned = redact(line)

    assert leaked not in cleaned
    assert "REDACTED" in cleaned


def test_redaction_keeps_ordinary_lines() -> None:
    line = "GET /api/orders 500 in 120ms: connection refused by db:5432"

    assert redact(line) == line
    assert REDACTED == "[REDACTED]"


def _streams(*values: tuple[int, str], labels: dict[str, str] | None = None) -> dict[str, object]:
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": labels
                    or {"container": "shop-api-1", "level": "error", "filename": "x"},
                    "values": [[str(ts), line] for ts, line in values],
                }
            ],
        },
    }


@respx.mock
async def test_query_range_returns_the_newest_lines_oldest_first() -> None:
    base = int(START.timestamp() * 1_000_000_000)
    route = respx.get(QUERY_RANGE).mock(
        return_value=httpx.Response(
            200, json=_streams((base + 3, "third"), (base + 1, "first"), (base + 2, "second"))
        )
    )
    async with httpx.AsyncClient() as http:
        lines = await LokiClient(LOKI, http).query_range('{container="shop-api-1"}', START, END, 2)

    assert [line.line for line in lines] == ["second", "third"]
    assert lines[0].labels == {"container": "shop-api-1", "level": "error"}
    params = route.calls.last.request.url.params
    assert params["query"] == '{container="shop-api-1"}'
    assert params["direction"] == "backward"
    assert params["start"] == str(base)


@pytest.mark.parametrize("status", [429, 500, 503])
@respx.mock
async def test_overloaded_or_failing_loki_is_retryable(status: int) -> None:
    respx.get(QUERY_RANGE).mock(return_value=httpx.Response(status))
    async with httpx.AsyncClient() as http:
        with pytest.raises(LokiUnavailableError, match=str(status)):
            await LokiClient(LOKI, http).query_range("{}", START, END, 10)


@respx.mock
async def test_unreachable_loki_is_retryable() -> None:
    respx.get(QUERY_RANGE).mock(side_effect=httpx.ConnectError("refused"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(LokiUnavailableError, match="ConnectError"):
            await LokiClient(LOKI, http).query_range("{}", START, END, 10)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, text="parse error"),
        httpx.Response(200, json={"data": {"resultType": "matrix", "result": []}}),
        httpx.Response(200, text="not json"),
    ],
)
@respx.mock
async def test_rejected_queries_and_garbage_are_not_retried(response: httpx.Response) -> None:
    respx.get(QUERY_RANGE).mock(return_value=response)
    async with httpx.AsyncClient() as http:
        with pytest.raises(LokiQueryError):
            await LokiClient(LOKI, http).query_range("{}", START, END, 10)
