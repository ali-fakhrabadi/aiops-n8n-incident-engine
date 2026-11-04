"""Analysers: Ollama (local), any OpenAI-compatible API, and a deterministic mock.

Both LLM providers are asked for output constrained to `OUTPUT_SCHEMA` at temperature 0
with a fixed seed, so a rerun on the same input gives the same answer where the backend
allows it. Their errors fall in two classes: `AnalyzerUnavailableError` (network, 5xx,
429: the stream retries the message later) and `AnalyzerError` (the request or the
answer is unusable: retrying would only repeat it).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Protocol

import httpx
from pydantic import SecretStr, ValidationError

from aiops.analysis.mock import mock_analysis
from aiops.analysis.prompt import AnalysisRequest
from aiops.analysis.schema import OUTPUT_SCHEMA, LlmAnalysis
from aiops.config import WorkerSettings

SEED = 7


class AnalyzerUnavailableError(Exception):
    """The analyser cannot answer now: retry later."""


class AnalyzerError(Exception):
    """The analyser rejected the request or answered unusably: do not retry."""


@dataclass(frozen=True)
class AnalyzerResult:
    analysis: LlmAnalysis
    provider: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class Analyzer(Protocol):
    provider: str
    model: str

    async def analyze(self, request: AnalysisRequest) -> AnalyzerResult: ...


def _parse(content: str) -> LlmAnalysis:
    try:
        return LlmAnalysis.model_validate(json.loads(content))
    except (ValueError, ValidationError) as exc:
        detail = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else "not JSON"
        msg = f"the model's answer does not match the schema: {detail}"
        raise AnalyzerError(msg) from exc


async def _post(
    http: httpx.AsyncClient, url: str, body: dict[str, Any], headers: dict[str, str]
) -> Any:
    try:
        response = await http.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        msg = f"cannot reach the model: {type(exc).__name__}"
        raise AnalyzerUnavailableError(msg) from exc
    if response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR or response.status_code in (
        HTTPStatus.TOO_MANY_REQUESTS,
        HTTPStatus.REQUEST_TIMEOUT,
    ):
        msg = f"the model answered HTTP {response.status_code}"
        raise AnalyzerUnavailableError(msg)
    if response.status_code != HTTPStatus.OK:
        msg = f"the model rejected the request: HTTP {response.status_code} {response.text[:200]}"
        raise AnalyzerError(msg)
    try:
        return response.json()
    except ValueError as exc:
        msg = "the model's response is not JSON"
        raise AnalyzerError(msg) from exc


class OllamaAnalyzer:
    provider = "ollama"

    def __init__(self, base_url: str, model: str, http: httpx.AsyncClient, *, num_ctx: int) -> None:
        self._url = f"{base_url.rstrip('/')}/api/chat"
        self.model = model
        self._http = http
        self._num_ctx = num_ctx

    async def analyze(self, request: AnalysisRequest) -> AnalyzerResult:
        body = {
            "model": self.model,
            "messages": list(request.messages),
            "stream": False,
            "format": OUTPUT_SCHEMA,
            "options": {"temperature": 0, "seed": SEED, "num_ctx": self._num_ctx},
            # Keep the model loaded between incidents; loading takes longer than answering.
            "keep_alive": "30m",
        }
        answer = await _post(self._http, self._url, body, {})
        try:
            content = answer["message"]["content"]
        except (KeyError, TypeError) as exc:
            msg = "Ollama's response has no message"
            raise AnalyzerError(msg) from exc
        return AnalyzerResult(
            analysis=_parse(str(content)),
            provider=self.provider,
            model=str(answer.get("model", self.model)),
            prompt_tokens=answer.get("prompt_eval_count"),
            completion_tokens=answer.get("eval_count"),
        )


class OpenAIAnalyzer:
    """OpenAI's chat completions API with strict structured output.

    The base URL makes it work with any compatible server (vLLM, LM Studio, a gateway).
    """

    provider = "openai"

    def __init__(
        self, base_url: str, model: str, api_key: SecretStr, http: httpx.AsyncClient
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/v1/chat/completions"
        self.model = model
        self._key = api_key
        self._http = http

    async def analyze(self, request: AnalysisRequest) -> AnalyzerResult:
        body = {
            "model": self.model,
            "messages": list(request.messages),
            "temperature": 0,
            "seed": SEED,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "incident_analysis",
                    "strict": True,
                    "schema": OUTPUT_SCHEMA,
                },
            },
        }
        headers = {"Authorization": f"Bearer {self._key.get_secret_value()}"}
        answer = await _post(self._http, self._url, body, headers)
        try:
            message = answer["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            msg = "the response has no choices"
            raise AnalyzerError(msg) from exc
        if message.get("refusal"):
            msg = f"the model refused: {str(message['refusal'])[:200]}"
            raise AnalyzerError(msg)
        usage = answer.get("usage") or {}
        return AnalyzerResult(
            analysis=_parse(str(message.get("content") or "")),
            provider=self.provider,
            model=str(answer.get("model", self.model)),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )


class MockAnalyzer:
    """Rule-based and deterministic: CI and reviewers run the pipeline without a model."""

    provider = "mock"
    model = "rules-v1"

    async def analyze(self, request: AnalysisRequest) -> AnalyzerResult:
        return AnalyzerResult(
            analysis=mock_analysis(request), provider=self.provider, model=self.model
        )


def build_analyzer(settings: WorkerSettings, http: httpx.AsyncClient) -> Analyzer:
    """The analyser the settings ask for; `http` must allow llm_timeout_seconds."""
    if settings.analyzer == "ollama":
        return OllamaAnalyzer(
            settings.ollama_url, settings.llm_model, http, num_ctx=settings.llm_num_ctx
        )
    if settings.analyzer == "openai":
        if settings.openai_api_key is None:  # pragma: no cover - rejected by the settings
            msg = "analyzer 'openai' needs an API key"
            raise ValueError(msg)
        return OpenAIAnalyzer(
            settings.openai_base_url, settings.llm_model, settings.openai_api_key, http
        )
    return MockAnalyzer()
