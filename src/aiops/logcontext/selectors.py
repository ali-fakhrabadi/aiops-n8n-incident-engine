"""Map an alert's labels to the Loki log streams of the thing that alerted.

Rules are tried in order; the first one whose labels are all present wins. They match
the labels of the observability stack this engine is built for (Docker Compose
containers shipped by Grafana Alloy) and work for any Loki with the same conventions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class SelectorRule:
    """Alert label -> Loki stream label pairs that identify the same workload."""

    name: str
    labels: tuple[tuple[str, str], ...]


RULES: tuple[SelectorRule, ...] = (
    # Docker state exporter and Loki ruler alerts carry the Compose labels directly.
    SelectorRule(
        "compose", (("compose_project", "compose_project"), ("compose_service", "compose_service"))
    ),
    # cAdvisor alerts carry them with cAdvisor's label names.
    SelectorRule(
        "cadvisor",
        (
            ("container_label_com_docker_compose_project", "compose_project"),
            ("container_label_com_docker_compose_service", "compose_service"),
        ),
    ),
    # Container name, as cAdvisor and the Docker state exporter label it.
    SelectorRule("container", (("name", "container"),)),
    # A scrape target named after its Compose service (TargetDown for "grafana").
    SelectorRule("job", (("job", "compose_service"),)),
)


def _quote(value: str) -> str:
    """A LogQL string literal; label values are attacker-influenced input."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


@dataclass(frozen=True)
class StreamSelector:
    rule: str
    query: str


def selector_for(labels: Mapping[str, str]) -> StreamSelector | None:
    for rule in RULES:
        values = [labels.get(alert_label) for alert_label, _ in rule.labels]
        if all(values):
            matchers = ", ".join(
                f"{loki_label}={_quote(value)}"
                for (_, loki_label), value in zip(rule.labels, values, strict=True)
                if value
            )
            return StreamSelector(rule=rule.name, query=f"{{{matchers}}}")
    return None
