#!/usr/bin/env python3
"""README screenshots: the n8n workflow, one of its real executions, and the API docs.

Runs in the image built from tools/screenshots (`make screenshots`) on the host network.
Signs in to n8n as its owner with the password from the secret file (never printed), and
picks the newest successful execution that sent a Telegram message. Only canvases are
captured: no node is opened, so no alert, log line or setting appears in the images.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import APIRequestContext, Page, sync_playwright

TELEGRAM_NODES = ("Telegram: page on-call", "Telegram: quiet")


def sign_in(request: APIRequestContext, n8n: str, email: str, password: str) -> bool:
    # n8n 2.x names the field emailOrLdapLoginId; 1.x called it email.
    for field in ("emailOrLdapLoginId", "email"):
        response = request.post(f"{n8n}/rest/login", data={field: email, "password": password})
        if response.ok:
            return True
    return False


def telegram_execution(request: APIRequestContext, n8n: str, workflow: str) -> str | None:
    """The newest successful execution in which a Telegram node ran."""
    listing = request.get(
        f"{n8n}/rest/executions",
        params={
            "filter": json.dumps({"workflowId": workflow, "status": ["success"]}),
            "limit": "30",
        },
    )
    if not listing.ok:
        return None
    body: Any = listing.json()
    results = body["data"]["results"] if "results" in body.get("data", {}) else body["data"]
    for execution in results:
        detail = request.get(f"{n8n}/rest/executions/{execution['id']}")
        if detail.ok and any(node in detail.text() for node in TELEGRAM_NODES):
            return str(execution["id"])
    return None


def canvas(page: Page, url: str, path: Path) -> None:
    page.goto(url, wait_until="networkidle")
    page.wait_for_selector(".vue-flow__node", timeout=30_000)
    page.wait_for_timeout(1500)
    page.mouse.click(5, 300)  # focus the canvas (an empty spot), not a node
    page.keyboard.press("1")  # zoom to fit
    page.wait_for_timeout(1500)
    page.screenshot(path=path)
    print(f"saved {path.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n8n", default="http://127.0.0.1:5678")
    parser.add_argument("--api", default="http://127.0.0.1:8020")
    parser.add_argument("--email", default="owner@example.com")
    parser.add_argument(
        "--password-file", type=Path, default=Path("/run/secrets/n8n_owner_password")
    )
    parser.add_argument("--workflow", default="aiopsIncRouting1")
    parser.add_argument("--out", type=Path, default=Path("/out"))
    args = parser.parse_args()
    password = args.password_file.read_text().strip()
    args.out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        # Twice the pixels, so the canvas' small print stays readable.
        context = browser.new_context(
            viewport={"width": 1600, "height": 900}, device_scale_factor=2
        )
        page = context.new_page()

        page.goto(f"{args.api}/docs", wait_until="networkidle")
        page.wait_for_selector(".opblock", timeout=30_000)
        page.screenshot(path=args.out / "api-docs.png")  # the endpoints, not every schema
        print("saved api-docs.png")

        if not sign_in(context.request, args.n8n, args.email, password):
            print("n8n sign-in failed (owner e-mail or password)", file=sys.stderr)
            return 1
        canvas(page, f"{args.n8n}/workflow/{args.workflow}", args.out / "n8n-workflow.png")
        execution = telegram_execution(context.request, args.n8n, args.workflow)
        if execution is None:
            print("no successful execution with a Telegram message yet", file=sys.stderr)
            return 1
        canvas(
            page,
            f"{args.n8n}/workflow/{args.workflow}/executions/{execution}",
            args.out / "n8n-execution.png",
        )
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
