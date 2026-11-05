#!/usr/bin/env python3
"""Build n8n_workflows/aiops_incident_routing.json from this definition and the code nodes.

The Code nodes' JavaScript lives in n8n_workflows/code/ (tested with `node --test`);
this script embeds it into an import-ready workflow. Node IDs are derived from node
names, so the output is the same every time. `make workflow` runs it; a test fails when
the committed JSON is out of date.

    python3 scripts/build_workflow.py [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CODE = ROOT / "n8n_workflows" / "code"
OUTPUT = ROOT / "n8n_workflows" / "aiops_incident_routing.json"

WORKFLOW_ID = "aiopsIncRouting1"
WEBHOOK_PATH = "aiops-incident"
CHAT_ID_PLACEHOLDER = "SET_YOUR_TELEGRAM_CHAT_ID"
DEFAULT_API_URL = "http://api:8000"
# Credentials the workflow refers to; scripts docker/n8n/setup.mjs creates them.
JWT = {"id": "aiopsWebhookJwt1", "name": "AIOps webhook signature"}
CALLBACK = {"id": "aiopsCallbackTk1", "name": "AIOps delivery callback"}
TELEGRAM = {"id": "aiopsTelegramBt1", "name": "Telegram bot"}

_NAMESPACE = uuid.UUID("6f1d4c2e-3b7a-4e58-9c0d-a1b2c3d4e5f6")
SETTINGS_EXPR = "$('Settings').first().json"
VERIFIED_EXPR = "$('Verify signed payload').first().json"


def _id(name: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, name))


def node(
    name: str,
    kind: str,
    version: float,
    position: tuple[int, int],
    parameters: dict[str, Any],
    **extra: Any,
) -> dict[str, Any]:
    return {
        "parameters": parameters,
        "id": _id(name),
        "name": name,
        "type": f"n8n-nodes-base.{kind}",
        "typeVersion": version,
        "position": list(position),
        **extra,
    }


def note(
    name: str, position: tuple[int, int], size: tuple[int, int], content: str
) -> dict[str, Any]:
    width, height = size
    return node(
        name,
        "stickyNote",
        1,
        position,
        {"content": content, "width": width, "height": height, "color": 7},
    )


def condition(name: str, left: str, operator: dict[str, Any], right: Any = "") -> dict[str, Any]:
    """A filter (If and Switch nodes) with one condition."""
    return {
        "options": {
            "caseSensitive": True,
            "leftValue": "",
            "typeValidation": "strict",
            "version": 2,
        },
        "conditions": [
            {"id": _id(name), "leftValue": left, "rightValue": right, "operator": operator}
        ],
        "combinator": "and",
    }


def code(file: str) -> str:
    return (CODE / file).read_text(encoding="utf-8")


def callback_request(url_tail: str, body: str, *, full_response: bool) -> dict[str, Any]:
    options: dict[str, Any] = {"timeout": 10000}
    if full_response:
        # A 409 (already claimed) is an answer, not an error.
        options["response"] = {"response": {"fullResponse": True, "neverError": True}}
    return {
        "method": "POST",
        "url": f"={{{{ {SETTINGS_EXPR}.aiops_api_url }}}}/api/v1/notifications/{url_tail}/delivery",
        "authentication": "genericCredentialType",
        "genericAuthType": "httpHeaderAuth",
        "sendBody": True,
        "specifyBody": "json",
        "jsonBody": body,
        "options": options,
    }


def telegram(
    name: str, position: tuple[int, int], *, silent: bool, reply: bool = False
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "appendAttribution": False,
        "disable_notification": silent,
        "disable_web_page_preview": True,
        "parse_mode": "HTML",
    }
    if reply:
        # The analysis answers the page it follows, so the two stay together in the chat.
        fields["reply_to_message_id"] = "={{ $json.reply_to_message_id }}"
    return node(
        name,
        "telegram",
        1.2,
        position,
        {
            "resource": "message",
            "operation": "sendMessage",
            "chatId": f"={{{{ {SETTINGS_EXPR}.telegram_chat_id }}}}",
            "text": "={{ $json.text }}",
            "additionalFields": fields,
        },
        credentials={"telegramApi": TELEGRAM},
        # Telegram through a slow link: n8n waits minutes for an answer, then retries.
        retryOnFail=True,
        maxTries=3,
        waitBetweenTries=5000,
        onError="continueErrorOutput",
    )


def route_rule(output: str) -> dict[str, Any]:
    return {
        "conditions": condition(
            f"route-{output}",
            "={{ $json.route }}",
            {"type": "string", "operation": "equals"},
            output,
        ),
        "renameOutput": True,
        "outputKey": output,
    }


def nodes() -> list[dict[str, Any]]:
    boolean_true = {"type": "boolean", "operation": "true", "singleValue": True}
    return [
        note(
            "Note: signed webhook",
            (-40, -200),
            (920, 300),
            "## 1. Signed webhook\n"
            "The AIOps worker posts every incident here. The **Webhook** node checks the "
            "HS256 token against the *AIOps webhook signature* credential (signature, "
            "expiry); **Verify signed payload** checks that the token was issued for this "
            "exact payload, notification and incident. Invalid requests get 400, valid "
            "ones 202 before anything is sent.",
        ),
        note(
            "Note: claim and route",
            (1080, -360),
            (1080, 300),
            "## 2. Claim, route, send\n"
            "The engine is asked first: a notification can be claimed once, so a "
            "repeated webhook never sends twice. A critical alert pages on-call with "
            "sound at once, before any analysis; the analysis follows silently, as a "
            "reply to the page. Other critical or warning alerts (resolved, flapping) "
            "are sent silently; info is recorded only. Telegram calls are retried 3 "
            "times, 5 s apart.",
        ),
        note(
            "Note: report back",
            (2200, -200),
            (460, 300),
            "## 3. Report back\n"
            "Every outcome (delivered with Telegram's message ID, failed with the reason, "
            "suppressed) goes back to the engine, which stores it with the incident and "
            "measures the time from alert to message.",
        ),
        node(
            "Incident webhook",
            "webhook",
            2.1,
            (0, 300),
            {
                "httpMethod": "POST",
                "path": WEBHOOK_PATH,
                "authentication": "jwtAuth",
                "responseMode": "responseNode",
                "options": {},
            },
            webhookId=_id("webhook-id"),
            credentials={"jwtAuth": JWT},
        ),
        node(
            "Settings",
            "set",
            3.4,
            (220, 300),
            {
                "assignments": {
                    "assignments": [
                        {
                            "id": _id("telegram_chat_id"),
                            "name": "telegram_chat_id",
                            "value": CHAT_ID_PLACEHOLDER,
                            "type": "string",
                        },
                        {
                            "id": _id("aiops_api_url"),
                            "name": "aiops_api_url",
                            "value": DEFAULT_API_URL,
                            "type": "string",
                        },
                    ]
                },
                "options": {},
            },
        ),
        node(
            "Verify signed payload",
            "code",
            2,
            (440, 300),
            {"jsCode": code("verify_payload.js")},
        ),
        node(
            "Valid?",
            "if",
            2.2,
            (660, 300),
            {"conditions": condition("valid", "={{ $json.valid }}", boolean_true), "options": {}},
        ),
        node(
            "Accept",
            "respondToWebhook",
            1.1,
            (880, 200),
            {
                "respondWith": "json",
                "responseBody": '{"accepted": true}',
                "options": {"responseCode": 202},
            },
        ),
        node(
            "Reject",
            "respondToWebhook",
            1.1,
            (880, 420),
            {
                "respondWith": "json",
                "responseBody": "={{ JSON.stringify({ error: $json.reason }) }}",
                "options": {"responseCode": 400},
            },
        ),
        node(
            "Claim delivery",
            "httpRequest",
            4.2,
            (1100, 200),
            callback_request(
                f"{{{{ {VERIFIED_EXPR}.notification.id }}}}",
                "={{ JSON.stringify({ status: 'processing', "
                "execution_id: String($execution.id) }) }}",
                full_response=True,
            ),
            credentials={"httpHeaderAuth": CALLBACK},
            retryOnFail=True,
            maxTries=3,
            waitBetweenTries=2000,
        ),
        node(
            "Claimed?",
            "if",
            2.2,
            (1320, 200),
            {
                "conditions": condition(
                    "claimed",
                    "={{ $json.statusCode }}",
                    {"type": "number", "operation": "equals"},
                    200,
                ),
                "options": {},
            },
        ),
        node("Already claimed", "noOp", 1, (1540, 360), {}),
        node(
            "Format message",
            "code",
            2,
            (1540, 100),
            {"jsCode": code("format_message.js")},
        ),
        node(
            "Route by severity",
            "switch",
            3.2,
            (1760, 100),
            {
                "rules": {
                    "values": [
                        route_rule(output) for output in ("page", "quiet", "reply", "suppress")
                    ]
                },
                "options": {},
            },
        ),
        telegram("Telegram: page on-call", (1980, -120), silent=False),
        telegram("Telegram: quiet", (1980, 60), silent=True),
        telegram("Telegram: analysis reply", (1980, 240), silent=True, reply=True),
        node(
            "Delivery outcome",
            "code",
            2,
            (2240, 100),
            {"mode": "runOnceForEachItem", "jsCode": code("delivery_outcome.js")},
        ),
        node(
            "Report delivery",
            "httpRequest",
            4.2,
            (2460, 100),
            callback_request(
                "{{ $json.notification_id }}",
                "={{ JSON.stringify($json.report) }}",
                full_response=False,
            ),
            credentials={"httpHeaderAuth": CALLBACK},
            retryOnFail=True,
            maxTries=5,
            waitBetweenTries=3000,
        ),
    ]


def _to(*targets: str) -> list[dict[str, Any]]:
    return [{"node": target, "type": "main", "index": 0} for target in targets]


def connections() -> dict[str, Any]:
    outcome = _to("Delivery outcome")
    return {
        "Incident webhook": {"main": [_to("Settings")]},
        "Settings": {"main": [_to("Verify signed payload")]},
        "Verify signed payload": {"main": [_to("Valid?")]},
        "Valid?": {"main": [_to("Accept"), _to("Reject")]},
        "Accept": {"main": [_to("Claim delivery")]},
        "Claim delivery": {"main": [_to("Claimed?")]},
        "Claimed?": {"main": [_to("Format message"), _to("Already claimed")]},
        "Format message": {"main": [_to("Route by severity")]},
        "Route by severity": {
            "main": [
                _to("Telegram: page on-call"),
                _to("Telegram: quiet"),
                _to("Telegram: analysis reply"),
                outcome,
            ]
        },
        # Output 0 is the answer, output 1 the error after the retries.
        "Telegram: page on-call": {"main": [outcome, outcome]},
        "Telegram: quiet": {"main": [outcome, outcome]},
        "Telegram: analysis reply": {"main": [outcome, outcome]},
        "Delivery outcome": {"main": [_to("Report delivery")]},
    }


def workflow() -> dict[str, Any]:
    return {
        "id": WORKFLOW_ID,
        "name": "AIOps incident routing",
        "nodes": nodes(),
        "connections": connections(),
        "active": False,
        "settings": {"executionOrder": "v1", "executionTimeout": 900},
        "pinData": {},
        "tags": [],
    }


def render() -> str:
    return json.dumps(workflow(), indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if the JSON is out of date")
    args = parser.parse_args()
    rendered = render()
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != rendered:
            print(f"{OUTPUT.relative_to(ROOT)} is out of date: run `make workflow`")
            return 1
        print(f"{OUTPUT.relative_to(ROOT)} is up to date")
        return 0
    OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
