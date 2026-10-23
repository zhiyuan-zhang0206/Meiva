# meiva:note
# What an admin events request asks of the event record: values in, the query out.
#
# `GET /api/cluster/admin/events` takes loose query-string filters. This module turns them into
# the one exact query the handler runs (`AdminEventsQuery`) or refuses them with a reason the
# handler answers as 400, so every rule about the filters is a value-in, plan-out assertion:
# the limit range, `agent_id` against `service_only`, the minimum level, the `since` window
# (relative or absolute), the comma-separated event names and the default 24-hour lower bound.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

__all__ = [
    "EVENTS_MAX_LIMIT",
    "AdminEventsError",
    "AdminEventsFilters",
    "AdminEventsQuery",
    "plan_admin_events",
]

# meiva:note
# Protective ceiling for the limit (the handler's range check); the default *window* is
# display.cluster_events_default_limit (task #3696 exception inventory: KEEP).
EVENTS_MAX_LIMIT = 1000

_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DEFAULT_WINDOW = timedelta(hours=24)


# meiva:note
# The request's filters cannot be satisfied; `str(exc)` is the 400 detail.
class AdminEventsError(Exception): ...


# meiva:note
# The query-string filters of one admin events request, as received.
@dataclass(frozen=True)
class AdminEventsFilters:
    agent_id: int | None
    service_only: bool
    level: str | None
    since: str | None
    event: str | None
    grep: str | None
    limit: int | None


# meiva:note
# The exact `telemetry_rows.query_events` filters one admin events request runs.
@dataclass(frozen=True)
class AdminEventsQuery:
    agent_id: int | None
    service_only: bool
    categories: tuple[str, ...]
    event_names: tuple[str, ...] | None
    level_min: str | None
    grep: str | None
    from_: datetime
    limit: int


def _level_min(level: str | None) -> str | None:
    if not level:
        return None
    upper = level.upper()
    if upper not in _LEVELS:
        raise AdminEventsError(f"unknown level: {level!r}")
    return upper.lower()


# meiva:note
# Seconds in `<number><s|m|h|d>`, or None when `text` is not that shape.
def _relative_amount(text: str) -> float | None:
    if text[-1] not in _UNIT_SECONDS:
        return None
    try:
        return float(text[:-1]) * _UNIT_SECONDS[text[-1]]
    except ValueError:
        return None


# meiva:note
# `<number><s|m|h|d>` is "this much time before `now`"; anything else is ISO-8601.
def _since(raw: str, now: datetime) -> datetime:
    text = raw.strip()
    if not text:
        raise AdminEventsError("since= empty")
    amount = _relative_amount(text)
    if amount is not None:
        return now - timedelta(seconds=amount)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdminEventsError(
            f"since={text!r} not a relative window (e.g. '2h') or ISO-8601 timestamp"
        ) from exc


# meiva:note
# The query for one request, or `AdminEventsError` naming the filter that is wrong.
def plan_admin_events(
    filters: AdminEventsFilters, *, default_limit: int, now: datetime
) -> AdminEventsQuery:
    limit = default_limit if filters.limit is None else filters.limit
    if limit < 1 or limit > EVENTS_MAX_LIMIT:
        raise AdminEventsError(f"limit must be in [1, {EVENTS_MAX_LIMIT}], got {limit}")
    if filters.agent_id is not None and filters.service_only:
        raise AdminEventsError("pass either `agent_id` or `service_only=true`, not both")
    names = (
        tuple(name.strip() for name in filters.event.split(",") if name.strip())
        if filters.event
        else None
    )
    return AdminEventsQuery(
        agent_id=filters.agent_id,
        service_only=filters.service_only,
        categories=("telemetry", "log"),
        event_names=names,
        level_min=_level_min(filters.level),
        grep=filters.grep,
        # meiva:note
        # A lower bound is always in effect; the default is the last 24 hours.
        from_=_since(filters.since, now) if filters.since else now - _DEFAULT_WINDOW,
        limit=limit,
    )
