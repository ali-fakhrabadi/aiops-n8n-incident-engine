#!/usr/bin/env python3
"""Run the evaluation scenarios through an analyser and report how it did (`make evaluate`).

For each scenario: how long the analyser took, whether its cause names the expected
problem, whether an injected instruction leaked into the output, and what the safety
checks removed (commands outside the allowlist, evidence not found in the logs). The
full analyses are written to a JSON file next to the table.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from aiops.analysis.providers import (
    Analyzer,
    AnalyzerError,
    AnalyzerUnavailableError,
    MockAnalyzer,
    OllamaAnalyzer,
    OpenAIAnalyzer,
)
from aiops.analysis.scenarios import Scenario, load_scenarios
from aiops.analysis.service import build_request, finalize

ROOT = Path(__file__).resolve().parent.parent


def _analyzer(args: argparse.Namespace, http: httpx.AsyncClient) -> Analyzer:
    if args.analyzer == "ollama":
        return OllamaAnalyzer(
            args.ollama_url, args.model, http, num_ctx=args.num_ctx, num_thread=args.num_thread
        )
    if args.analyzer == "openai":
        key = SecretStr(Path(args.openai_api_key_file).read_text(encoding="utf-8").strip())
        return OpenAIAnalyzer(args.openai_base_url, args.model, key, http)
    return MockAnalyzer()


async def _evaluate(analyzer: Analyzer, scenario: Scenario) -> dict[str, Any]:
    request = build_request(scenario.incident, max_lines=150, max_chars=12_000)
    started = time.perf_counter()
    try:
        result = await analyzer.analyze(request)
    except (AnalyzerError, AnalyzerUnavailableError) as exc:
        return {
            "scenario": scenario.name,
            "error": str(exc),
            "seconds": round(time.perf_counter() - started, 1),
        }
    seconds = time.perf_counter() - started
    stored = finalize(result, request, round(seconds * 1000))
    shown = " ".join(
        [stored["summary"], stored["probable_cause"], *stored["next_steps"]]
        + [command["command"] for command in stored["suggested_commands"]]
    ).casefold()
    return {
        "scenario": scenario.name,
        "seconds": round(seconds, 1),
        "confidence": stored["confidence"],
        "cause_matches": any(word in shown for word in scenario.keywords),
        "injection_followed": any(word.casefold() in shown for word in scenario.forbidden),
        "commands_kept": len(stored["suggested_commands"]),
        "commands_rejected": len(stored["rejected_commands"]),
        "evidence_kept": len(stored["evidence"]),
        "evidence_dropped": stored["evidence_dropped"],
        "analysis": stored,
    }


def _table(rows: list[dict[str, Any]]) -> str:
    columns = [
        "Scenario",
        "Seconds",
        "Confidence",
        "Cause matches",
        "Injection followed",
        "Commands kept / rejected",
        "Evidence kept / dropped",
    ]
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for row in rows:
        if "error" in row:
            lines.append(
                f"| {row['scenario']} | {row['seconds']} | error: {row['error'][:60]} | | | | |"
            )
            continue
        matched = "yes" if row["cause_matches"] else "no"
        followed = "YES" if row["injection_followed"] else "no"
        lines.append(
            f"| {row['scenario']} | {row['seconds']} | {row['confidence']} | {matched} | "
            f"{followed} | {row['commands_kept']} / {row['commands_rejected']} | "
            f"{row['evidence_kept']} / {row['evidence_dropped']} |"
        )
    return "\n".join(lines)


async def _main(args: argparse.Namespace) -> int:
    scenarios = load_scenarios(args.scenarios)
    async with httpx.AsyncClient(timeout=args.timeout) as http:
        analyzer = _analyzer(args, http)
        rows = []
        for scenario in scenarios:
            row = await _evaluate(analyzer, scenario)
            rows.append(row)
            print(f"{scenario.name}: {row.get('seconds')} s", file=sys.stderr, flush=True)
    report = {"analyzer": args.analyzer, "model": analyzer.model, "results": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Analyser: {args.analyzer} ({analyzer.model})\n")
    print(_table(rows))
    print(f"\nFull analyses: {args.output}")
    return 1 if any("error" in row for row in rows) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--analyzer", choices=["mock", "ollama", "openai"], default="mock")
    parser.add_argument("--model", default="llama3.1")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--num-ctx", type=int, default=8192)
    parser.add_argument("--num-thread", type=int, help="Ollama CPU threads (default: its own)")
    parser.add_argument("--openai-base-url", default="https://api.openai.com")
    parser.add_argument("--openai-api-key-file", default=str(ROOT / "secrets" / "openai_api_key"))
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--scenarios", type=Path, default=ROOT / "evaluation" / "scenarios.json")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "evaluation" / "results" / "latest.json"
    )
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
