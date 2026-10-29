"""Alertmanager webhook payload (version 4), validated with explicit size limits.

https://prometheus.io/docs/alerting/latest/configuration/#webhook_config
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from aiops.incidents.models import IncidentStatus, Severity

LabelName = Annotated[str, StringConstraints(min_length=1, max_length=128)]
LabelValue = Annotated[str, StringConstraints(max_length=1024)]
AnnotationValue = Annotated[str, StringConstraints(max_length=8192)]

Labels = Annotated[dict[LabelName, LabelValue], Field(max_length=64)]
Annotations = Annotated[dict[LabelName, AnnotationValue], Field(max_length=64)]

_SEVERITIES = {member.value for member in Severity} - {Severity.UNKNOWN.value}


def _zero_time_is_none(value: datetime | None) -> datetime | None:
    # Go's zero time, 0001-01-01T00:00:00Z, means "not set".
    if value is None or value.year <= 1:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class AlertmanagerAlert(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)

    status: IncidentStatus
    labels: Labels
    annotations: Annotations = Field(default_factory=dict)
    starts_at: datetime = Field(alias="startsAt")
    ends_at: datetime | None = Field(default=None, alias="endsAt")
    generator_url: Annotated[str, StringConstraints(max_length=4096)] = Field(
        default="", alias="generatorURL"
    )
    fingerprint: Annotated[str, StringConstraints(max_length=64, pattern=r"^[0-9a-fA-F]*$")] = ""

    @field_validator("labels")
    @classmethod
    def _has_alertname(cls, labels: dict[str, str]) -> dict[str, str]:
        if not labels.get("alertname"):
            msg = "an alert needs an 'alertname' label"
            raise ValueError(msg)
        return labels

    @field_validator("starts_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    @field_validator("ends_at")
    @classmethod
    def _zero_is_unset(cls, value: datetime | None) -> datetime | None:
        return _zero_time_is_none(value)

    @property
    def alertname(self) -> str:
        return self.labels["alertname"]

    @property
    def severity(self) -> Severity:
        value = self.labels.get("severity", "").lower()
        return Severity(value) if value in _SEVERITIES else Severity.UNKNOWN

    @property
    def identity(self) -> str:
        """Alertmanager's fingerprint, or the same idea computed from the labels."""
        if self.fingerprint:
            return self.fingerprint.lower()
        canonical = json.dumps(self.labels, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]


class AlertmanagerWebhook(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)

    version: Literal["4"]
    group_key: Annotated[str, StringConstraints(max_length=4096)] = Field(alias="groupKey")
    truncated_alerts: int = Field(default=0, ge=0, alias="truncatedAlerts")
    status: Literal["firing", "resolved"]
    receiver: Annotated[str, StringConstraints(max_length=256)]
    group_labels: Labels = Field(default_factory=dict, alias="groupLabels")
    common_labels: Labels = Field(default_factory=dict, alias="commonLabels")
    common_annotations: Annotations = Field(default_factory=dict, alias="commonAnnotations")
    external_url: Annotated[str, StringConstraints(max_length=4096)] = Field(
        default="", alias="externalURL"
    )
    alerts: Annotated[list[AlertmanagerAlert], Field(min_length=1, max_length=10_000)]
