from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr, ValidationError

from aiops.analysis.prompt import AnalysisRequest
from aiops.analysis.providers import (
    AnalyzerError,
    AnalyzerUnavailableError,
    MockAnalyzer,
    OllamaAnalyzer,
    OpenAIAnalyzer,
    build_analyzer,
)
from aiops.analysis.scenarios import load_scenarios
from aiops.analysis.schema import OUTPUT_SCHEMA, LlmAnalysis
from aiops.analysis.service import build_request
from aiops.config import WorkerSettings
from tests.conftest import ROOT

SCENARIOS = {item.name: item for item in load_scenarios(ROOT / "evaluation" / "scenarios.json")}
OLLAMA = "http://ollama.test:11434"
OPENAI = "https://llm.test"

VALID: dict[str, Any] = {
    "summary": "checkout cannot reach its database",
    "probable_cause": "PostgreSQL at db:5432 refuses connections.",
    "confidence": "high",
    "evidence": [{"quote": "Connection refused", "why": "the database is down"}],
    "suggested_commands": [{"command": "docker compose -p shop ps db", "purpose": "is it running"}],
    "next_steps": ["Start the database"],
}


def _request(name: str = "database-unreachable") -> AnalysisRequest:
    return build_request(SCENARIOS[name].incident, max_lines=150, max_chars=12_000)


def test_the_strict_schema_matches_the_model() -> None:
    assert set(OUTPUT_SCHEMA["required"]) == set(LlmAnalysis.model_fields)
    assert OUTPUT_SCHEMA["additionalProperties"] is False
    LlmAnalysis.model_validate(VALID)


def test_the_model_enforces_what_the_schema_cannot() -> None:
    too_many = {**VALID, "suggested_commands": [VALID["suggested_commands"][0]] * 7}

    with pytest.raises(ValidationError):
        LlmAnalysis.model_validate(too_many)
    with pytest.raises(ValidationError):
        LlmAnalysis.model_validate({**VALID, "unexpected": "field"})


@pytest.mark.parametrize(
    ("name", "cause_word", "confidence"),
    [
        ("out-of-memory", "memory", "medium"),
        ("database-unreachable", "unreachable", "medium"),
        ("disk-full", "full", "medium"),
        ("prompt-injection", "unreachable", "medium"),
        ("no-logs", "space", "low"),
    ],
)
async def test_the_mock_recognises_the_scenarios(
    name: str, cause_word: str, confidence: str
) -> None:
    result = await MockAnalyzer().analyze(_request(name))

    assert cause_word in result.analysis.probable_cause
    assert result.analysis.confidence == confidence
    assert (result.provider, result.model) == ("mock", "rules-v1")


async def test_the_mock_is_deterministic_and_quotes_only_real_lines() -> None:
    request = _request("out-of-memory")
    first = await MockAnalyzer().analyze(request)
    second = await MockAnalyzer().analyze(request)

    assert first == second
    texts = [line.text for line in request.logs.lines]
    assert all(any(item.quote in text for text in texts) for item in first.analysis.evidence)


@respx.mock
async def test_ollama_is_asked_for_schema_bound_deterministic_output() -> None:
    route = respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "llama3.1:latest",
                "message": {"role": "assistant", "content": json.dumps(VALID)},
                "prompt_eval_count": 812,
                "eval_count": 153,
            },
        )
    )
    async with httpx.AsyncClient() as http:
        result = await OllamaAnalyzer(OLLAMA, "llama3.1", http, num_ctx=8192).analyze(_request())

    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "llama3.1"
    assert body["format"] == OUTPUT_SCHEMA
    assert body["stream"] is False
    assert body["options"] == {"temperature": 0, "seed": 7, "num_ctx": 8192}
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    assert result.analysis.confidence == "high"
    assert (result.model, result.prompt_tokens, result.completion_tokens) == (
        "llama3.1:latest",
        812,
        153,
    )


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(503), AnalyzerUnavailableError),
        (httpx.Response(429), AnalyzerUnavailableError),
        (httpx.Response(404, json={"error": "model 'llama3.1' not found"}), AnalyzerError),
        (httpx.Response(200, json={"message": {"content": "not json at all"}}), AnalyzerError),
        (
            httpx.Response(200, json={"message": {"content": json.dumps({"summary": "x"})}}),
            AnalyzerError,
        ),
        (httpx.Response(200, json={"done": True}), AnalyzerError),
    ],
)
@respx.mock
async def test_ollama_failures_are_classified(
    response: httpx.Response, error: type[Exception]
) -> None:
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=response)
    async with httpx.AsyncClient() as http:
        with pytest.raises(error):
            await OllamaAnalyzer(OLLAMA, "llama3.1", http, num_ctx=8192).analyze(_request())


@respx.mock
async def test_an_unreachable_model_is_retryable() -> None:
    respx.post(f"{OLLAMA}/api/chat").mock(side_effect=httpx.ConnectTimeout("slow"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(AnalyzerUnavailableError, match="ConnectTimeout"):
            await OllamaAnalyzer(OLLAMA, "llama3.1", http, num_ctx=8192).analyze(_request())


@respx.mock
async def test_openai_uses_strict_structured_output_and_the_key() -> None:
    route = respx.post(f"{OPENAI}/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "gpt-test",
                "choices": [{"message": {"role": "assistant", "content": json.dumps(VALID)}}],
                "usage": {"prompt_tokens": 700, "completion_tokens": 90},
            },
        )
    )
    async with httpx.AsyncClient() as http:
        analyzer = OpenAIAnalyzer(OPENAI, "gpt-test", SecretStr("sk-test-key"), http)
        result = await analyzer.analyze(_request())

    request = route.calls.last.request
    body = json.loads(request.content)
    assert request.headers["authorization"] == "Bearer sk-test-key"
    assert body["response_format"]["json_schema"] == {
        "name": "incident_analysis",
        "strict": True,
        "schema": OUTPUT_SCHEMA,
    }
    assert body["temperature"] == 0
    assert (result.provider, result.prompt_tokens) == ("openai", 700)


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (
            httpx.Response(200, json={"choices": [{"message": {"refusal": "I can't help"}}]}),
            AnalyzerError,
        ),
        (httpx.Response(200, json={"choices": []}), AnalyzerError),
        (httpx.Response(401, json={"error": "bad key"}), AnalyzerError),
        (httpx.Response(500), AnalyzerUnavailableError),
        (httpx.Response(200, text="<html>gateway</html>"), AnalyzerError),
    ],
)
@respx.mock
async def test_openai_failures_are_classified(
    response: httpx.Response, error: type[Exception]
) -> None:
    respx.post(f"{OPENAI}/v1/chat/completions").mock(return_value=response)
    async with httpx.AsyncClient() as http:
        with pytest.raises(error):
            await OpenAIAnalyzer(OPENAI, "m", SecretStr("k"), http).analyze(_request())


def _settings(tmp_path: Path, **overrides: Any) -> WorkerSettings:
    values: dict[str, Any] = {
        "db_password": SecretStr("unused"),
        "db_password_file": None,
        "redis_password": None,
        "redis_password_file": None,
        "openai_api_key_file": None,
        "heartbeat_file": tmp_path / "hb",
        **overrides,
    }
    return WorkerSettings(**values)


async def test_the_settings_choose_the_analyzer(tmp_path: Path) -> None:
    async with httpx.AsyncClient() as http:
        assert build_analyzer(_settings(tmp_path), http).provider == "mock"
        assert build_analyzer(_settings(tmp_path, analyzer="ollama"), http).provider == "ollama"
        openai = _settings(tmp_path, analyzer="openai", openai_api_key=SecretStr("k"))
        assert build_analyzer(openai, http).provider == "openai"


def test_openai_without_a_key_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="needs openai_api_key"):
        _settings(tmp_path, analyzer="openai", openai_api_key=None)
    with pytest.raises(ValidationError, match="http"):
        _settings(tmp_path, ollama_url="ollama:11434")
