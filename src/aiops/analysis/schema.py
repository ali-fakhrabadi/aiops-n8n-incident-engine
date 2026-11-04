"""What an analyser must return, as a Pydantic model and as the JSON Schema sent to the LLM.

The JSON Schema is written out by hand rather than generated: Ollama's structured output
and OpenAI's strict mode both constrain generation to it, and strict mode accepts only a
subset (every property required, no additional properties, no length keywords). The
Pydantic model then enforces the limits the schema cannot express.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    quote: Short  # verbatim from a log line or an alert field; checked after generation
    why: Short


class SuggestedCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    purpose: Short


class LlmAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    probable_cause: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1500)
    ]
    confidence: Confidence
    evidence: Annotated[list[Evidence], Field(max_length=5)]
    suggested_commands: Annotated[list[SuggestedCommand], Field(max_length=6)]
    next_steps: Annotated[list[Short], Field(max_length=6)]


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_STRING = {"type": "string"}

OUTPUT_SCHEMA: dict[str, Any] = _object(
    {
        "summary": {**_STRING, "description": "One sentence: what is wrong."},
        "probable_cause": {
            **_STRING,
            "description": "The most likely cause, from the evidence only; say so if unclear.",
        },
        "confidence": {"type": "string", "enum": [level.value for level in Confidence]},
        "evidence": {
            "type": "array",
            "description": "Up to 5 exact quotes from the log lines or alert supporting the cause.",
            "items": _object({"quote": _STRING, "why": _STRING}),
        },
        "suggested_commands": {
            "type": "array",
            "description": "Up to 6 read-only diagnostic commands for a human to run.",
            "items": _object({"command": _STRING, "purpose": _STRING}),
        },
        "next_steps": {
            "type": "array",
            "description": "Up to 6 short next steps for the on-call engineer.",
            "items": _STRING,
        },
    }
)
