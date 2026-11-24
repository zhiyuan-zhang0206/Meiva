# meiva:link gateway/events/docs/event-reads.md

from __future__ import annotations

import json
from dataclasses import dataclass

from base.events.contract import TIER_BY_EVENT, EventGroupBy, EventTier
from base.telemetry.metrics.logql import EVENT_STREAM_MATCHER

__all__ = ["LokiFilters", "count_query", "log_query", "metric_group_label"]

_LEVELS = ("debug", "info", "warning", "error", "critical")
_ANOMALY_LEVELS = ("warning", "error", "critical")

_WRITE_PROBE_AGENT_ID = "watchdog"

_BODY_MACHINE = "event_machine"


@dataclass(frozen=True)
class LokiFilters:
    agent_id: int | None = None
    service_only: bool = False
    event_names: tuple[str, ...] | None = None
    tiers: tuple[EventTier, ...] | None = None
    level: str | None = None
    level_min: str | None = None
    grep: str | None = None
    categories: tuple[str, ...] | None = None
    machine: str | None = None
    trace_id: str | None = None


def _quoted(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


_REGEX_SPECIALS = frozenset("\\.+*?()|[]{}^$")


def _regex_escaped(text: str) -> str:
    return "".join(f"\\{char}" if char in _REGEX_SPECIALS else char for char in text)


def _alternatives(values: tuple[str, ...] | list[str]) -> str:
    return _quoted("^(?:" + "|".join(_regex_escaped(value) for value in values) + ")$")


def _names(tier: EventTier) -> list[str]:
    return sorted(name for name, declared in TIER_BY_EVENT.items() if declared == tier)


def tier_filter(tiers: tuple[EventTier, ...]) -> str | None:
    anomaly_level = f"level=~{_alternatives(_ANOMALY_LEVELS)}"
    quiet_level = f"level!~{_alternatives(_ANOMALY_LEVELS)}"
    clauses: list[str] = []
    for tier in dict.fromkeys(tiers):
        match tier:
            case "business":
                continue
            case "anomaly":
                clauses.append(f"({anomaly_level} or event_name=~{_alternatives(_names(tier))})")
            case "noise":
                clauses.append(f"({quiet_level} and event_name=~{_alternatives(_names(tier))})")
            case "observation":
                declared = sorted(
                    name for name, kind in TIER_BY_EVENT.items() if kind != "observation"
                )
                clauses.append(f"({quiet_level} and event_name!~{_alternatives(declared)})")
    return " or ".join(clauses) if clauses else None


def _selector(filters: LokiFilters) -> str:
    matchers = [EVENT_STREAM_MATCHER]
    if filters.agent_id is not None:
        matchers.append(f'agent_id="{filters.agent_id}"')
    elif filters.service_only:
        matchers.append('agent_id=""')
    else:
        matchers.append(f'agent_id!="{_WRITE_PROBE_AGENT_ID}"')
    if filters.event_names:
        if len(filters.event_names) == 1:
            matchers.append(f"event_name={_quoted(filters.event_names[0])}")
        else:
            matchers.append(f"event_name=~{_alternatives(filters.event_names)}")
    return "{" + ", ".join(matchers) + "}"


def _levels_from(level_min: str) -> list[str]:
    return list(_LEVELS[_LEVELS.index(level_min) :])


def _stages(filters: LokiFilters, *, tier: str | None) -> list[str]:
    stages: list[str] = []
    if filters.grep:
        stages.append(f"|~ {_quoted('(?i)' + _regex_escaped(filters.grep))}")
    if filters.categories:
        if len(filters.categories) == 1:
            stages.append(f"| category={_quoted(filters.categories[0])}")
        else:
            stages.append(f"| category=~{_alternatives(filters.categories)}")
    if filters.level is not None:
        stages.append(f"| level={_quoted(filters.level.lower())}")
    if filters.level_min is not None:
        stages.append(f"| level=~{_alternatives(_levels_from(filters.level_min))}")
    if filters.trace_id is not None:
        stages.append(f"| trace_id={_quoted(filters.trace_id)}")
    if tier is not None:
        stages.append(f"| {tier}")
    if filters.machine is not None:
        stages.append(
            f'| json {_BODY_MACHINE}="machine" | {_BODY_MACHINE}={_quoted(filters.machine)}'
        )
    return stages


def log_query(filters: LokiFilters) -> str | None:
    tier = None
    if filters.tiers is not None:
        tier = tier_filter(filters.tiers)
        if tier is None:
            return None
    return " ".join([_selector(filters), *_stages(filters, tier=tier)])


def metric_group_label(by: EventGroupBy) -> str:
    return _BODY_MACHINE if by == "machine" else by


def count_query(filters: LokiFilters, *, by: EventGroupBy | None, range_ms: int) -> str | None:
    pipeline = log_query(filters)
    if pipeline is None:
        return None
    label = None if by is None else metric_group_label(by)
    if by == "machine" and filters.machine is None:
        pipeline += f' | json {_BODY_MACHINE}="machine"'
    elif by in ("level", "category"):
        pipeline += f' | {label}=~".*"'
    counted = f"count_over_time({pipeline} [{range_ms}ms])"
    return f"sum({counted})" if label is None else f"sum by ({label}) ({counted})"
