"""The n8n workflow agrees with the engine: webhook, token claims, credentials, routes."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

from aiops.notifications.payload import PAYLOAD_VERSION
from aiops.notifications.signing import AUDIENCE, ISSUER

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / "n8n_workflows" / "aiops_incident_routing.json"
CODE = ROOT / "n8n_workflows" / "code"


def _workflow() -> dict[str, Any]:
    workflow: dict[str, Any] = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    return workflow


def _nodes() -> dict[str, dict[str, Any]]:
    return {node["name"]: node for node in _workflow()["nodes"]}


def test_the_committed_workflow_is_up_to_date() -> None:
    spec = importlib.util.spec_from_file_location(
        "build_workflow", ROOT / "scripts" / "build_workflow.py"
    )
    assert spec is not None
    assert spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    assert WORKFLOW.read_text(encoding="utf-8") == builder.render(), "run `make workflow`"


def test_connections_join_existing_nodes() -> None:
    nodes = _nodes()
    for source, outputs in _workflow()["connections"].items():
        assert source in nodes
        for output in outputs["main"]:
            for link in output:
                assert link["node"] in nodes, f"{source} -> {link['node']}"


def test_the_webhook_is_the_one_the_worker_calls() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    default = re.search(r"AIOPS_N8N_WEBHOOK_URL-http://n8n:5678/webhook/([\w-]+)\}", compose)
    webhook = _nodes()["Incident webhook"]["parameters"]

    assert default is not None
    assert webhook["path"] == default.group(1)
    assert webhook["httpMethod"] == "POST"
    assert webhook["authentication"] == "jwtAuth"
    assert webhook["responseMode"] == "responseNode"


def test_the_signature_check_expects_the_engine_s_claims() -> None:
    verify = (CODE / "verify_payload.js").read_text(encoding="utf-8")

    assert f"const ISSUER = '{ISSUER}';" in verify
    assert f"const AUDIENCE = '{AUDIENCE}';" in verify
    assert f"const PAYLOAD_VERSION = {PAYLOAD_VERSION};" in verify


def test_telegram_is_retried_and_reports_its_errors() -> None:
    for name in ("Telegram: page on-call", "Telegram: quiet"):
        node = _nodes()[name]
        fields = node["parameters"]["additionalFields"]
        assert node["retryOnFail"] is True
        assert node["maxTries"] >= 3
        assert node["waitBetweenTries"] >= 1000
        assert node["onError"] == "continueErrorOutput"
        assert fields["appendAttribution"] is False
        assert fields["parse_mode"] == "HTML"
        assert "$('Settings')" in node["parameters"]["chatId"]
    assert _nodes()["Telegram: quiet"]["parameters"]["additionalFields"]["disable_notification"]


def test_no_chat_id_or_secret_is_committed() -> None:
    settings = _nodes()["Settings"]["parameters"]["assignments"]["assignments"]
    raw = WORKFLOW.read_text(encoding="utf-8")

    assert {item["name"]: item["value"] for item in settings}["telegram_chat_id"] == (
        "SET_YOUR_TELEGRAM_CHAT_ID"
    )
    assert re.search(r"\d{5,}:[A-Za-z0-9_-]{30,}", raw) is None  # a bot token
    assert "Bearer " not in raw


def test_every_credential_is_one_the_setup_creates() -> None:
    setup = (ROOT / "docker" / "n8n" / "setup.mjs").read_text(encoding="utf-8")
    types = {kind for node in _workflow()["nodes"] for kind in node.get("credentials", {})}

    assert types == {"jwtAuth", "httpHeaderAuth", "telegramApi"}
    for kind in types:
        assert f"case '{kind}':" in setup


def test_every_route_has_its_branch() -> None:
    nodes = _nodes()
    rules = nodes["Route by severity"]["parameters"]["rules"]["values"]
    branches = _workflow()["connections"]["Route by severity"]["main"]
    formatter = (CODE / "format_message.js").read_text(encoding="utf-8")

    assert [rule["outputKey"] for rule in rules] == ["page", "quiet", "suppress"]
    assert [branch[0]["node"] for branch in branches] == [
        "Telegram: page on-call",
        "Telegram: quiet",
        "Delivery outcome",
    ]
    for route in ("page", "quiet", "suppress"):
        assert f"return '{route}';" in formatter
