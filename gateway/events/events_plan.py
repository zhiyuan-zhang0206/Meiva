# meiva:note
# What a `GET /api/events` request asks of the event stores: values in, a plan out.
#
# The route (`gateway.events.router`) owns the HTTP parameters, the connection and the merge of
# two stores' pages. This module owns every decision that needs none of that: which filters apply
# (validated and normalized), which stores are read, what window and page, and how deep each
# store is read. A test passes the request values and a `now`, and asserts the plan.

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from base.events.contract import EVENTS, EventTier

__all__ = [
    "AgentEventsQuery",
    "AgentEventsRequest",
    "EventsFilters",
    "EventsPlan",
    "EventsRequest",
    "InvalidEventsQueryError",
    "page_depth",
    "plan_agent_events",
    "plan_events",
]

# meiva:note
# These are stored lowercase (design doc §1); unknown values are
# rejected with 422 rather than silently matching nothing (fail fast).
_CATEGORIES = frozenset({"audit", "telemetry", "log"})
_LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})
_TIERS: tuple[EventTier, ...] = ("business", "anomaly", "observation", "noise")
_IMPERSONATION_SESSION = re.compile(r"^[0-9]+:[0-9]+$")

# meiva:note
# Default window when the request names no lower bound (`from`/`hours`);
# it bounds the count/list fetch.
_DEFAULT_WINDOW_HOURS = 24


# meiva:note
# The request names an illegal filter or window; the message is the 422 detail.
class InvalidEventsQueryError(ValueError): ...


# meiva:note
# The query parameters of one request, as the route received them.
@dataclass(frozen=True)
class EventsRequest:
    category: str | None
    event_name: str | None
    agent_id: int | None
    trace_id: str | None
    impersonation_session: str | None
    machine: str | None
    level: str | None
    tier: str | None
    from_: datetime | None
    to: datetime | None
    hours: float | None
    limit: int | None
    offset: int
    with_total: bool


# meiva:note
# The filters every store applies, already validated and normalized.
@dataclass(frozen=True)
class EventsFilters:
    agent_id: int | None
    event_names: list[str] | None
    tiers: list[EventTier] | None
    trace_id: str | None
    machine: str | None
    level: str | None
    attribute_filters: dict[str, str] | None
    from_: datetime | None
    to: datetime | None


# meiva:note
# The stores to read, with the filters, the window and the page.
@dataclass(frozen=True)
class EventsPlan:
    filters: EventsFilters
    use_audit: bool
    use_telemetry: bool
    telemetry_categories: list[str]
    limit: int
    offset: int
    with_total: bool


def _normalized_level(level: str | None) -> str | None:
    if level is None:
        return None
    level = level.lower()
    if level not in _LEVELS:
        raise InvalidEventsQueryError(f"level must be one of {sorted(_LEVELS)}, got {level!r}")
    return level


# meiva:note
# Return the normalized `level` (lowercase) or raise. `from` + `hours` together is a
# contradiction — reject it instead of silently picking one. Naive timestamps are rejected
# too: a tz-less `from`/`to` would be interpreted in the server's local timezone, silently
# shifting the window — fail fast instead of guessing.
def _validate(request: EventsRequest) -> str | None:
    if request.category is not None and request.category not in _CATEGORIES:
        raise InvalidEventsQueryError(
            f"category must be one of {sorted(_CATEGORIES)}, got {request.category!r}"
        )
    level = _normalized_level(request.level)
    for name, value in (("from", request.from_), ("to", request.to)):
        if value is not None and value.tzinfo is None:
            raise InvalidEventsQueryError(
                f"{name} must include a timezone offset "
                f"(e.g. 2026-08-04T00:00:00Z) — naive timestamps are "
                f"interpreted in the server's local timezone"
            )
    if request.from_ is not None and request.hours is not None:
        raise InvalidEventsQueryError(
            "from and hours are mutually exclusive — pass one time window"
        )
    return level


# meiva:note
# Normalize a comma-separated tier filter or fail fast.
def _parse_tiers(tier: str | None) -> list[EventTier] | None:
    if tier is None:
        return None
    tiers: list[EventTier] = []
    for raw in tier.split(","):
        value = raw.strip().lower()
        if value not in _TIERS:
            raise InvalidEventsQueryError(
                f"tier must be a comma-separated list of {_TIERS}, got {tier!r}"
            )
        if value not in tiers:
            tiers.append(value)
    return tiers


# meiva:note
# Validate the private replay correlation value and build its attribute filter.
def _impersonation_filters(session: str | None) -> dict[str, str] | None:
    if session is None:
        return None
    if not _IMPERSONATION_SESSION.fullmatch(session):
        raise InvalidEventsQueryError("impersonation_session must be an agent_id:session_id pair")
    return {"impersonation_session": session}


# meiva:note
# Which tables a request reads: `(audit_events, telemetry_events)`.
#
# An explicit category picks one table. Without one, a registered event name
# picks the table(s) its declared categories live in; anything else reads both.
def _sources(category: str | None, event_name: str | None) -> tuple[bool, bool]:
    if category is not None:
        return category == "audit", category != "audit"
    spec = EVENTS.get(event_name) if event_name is not None else None
    if spec is None:
        return True, True
    categories = {spec.category, *spec.extra_categories}
    return "audit" in categories, bool(categories - {"audit"})


# meiva:note
# Validate a request and resolve its stores, filters, window and page.
#
# A lower bound is always in effect: absent both `from` and `hours`, the last 24 hours are
# assumed, so the API never runs an unbounded window. Raises `InvalidEventsQueryError`.
def plan_events(request: EventsRequest, *, now: datetime, default_limit: int) -> EventsPlan:
    level = _validate(request)
    tiers = _parse_tiers(request.tier)
    attribute_filters = _impersonation_filters(request.impersonation_session)
    window_from = request.from_
    if request.hours is not None:
        window_from = now - timedelta(hours=request.hours)
    if window_from is None:
        window_from = now - timedelta(hours=_DEFAULT_WINDOW_HOURS)
    use_audit, use_telemetry = _sources(request.category, request.event_name)
    return EventsPlan(
        filters=EventsFilters(
            agent_id=request.agent_id,
            event_names=[request.event_name] if request.event_name is not None else None,
            tiers=tiers,
            trace_id=request.trace_id.lower() if request.trace_id is not None else None,
            machine=request.machine,
            level=level,
            attribute_filters=attribute_filters,
            from_=window_from,
            to=request.to,
        ),
        use_audit=use_audit,
        use_telemetry=use_telemetry,
        telemetry_categories=[request.category]
        if request.category is not None
        else ["telemetry", "log"],
        limit=request.limit if request.limit is not None else default_limit,
        offset=request.offset,
        with_total=request.with_total,
    )


# meiva:note
# The `(limit, offset)` each store is read with.
#
# One store pages directly. Two stores each give their newest `offset + limit` rows, which are
# merged and then sliced: a global page never needs a row beyond that depth from either side.
def page_depth(plan: EventsPlan) -> tuple[int, int]:
    if plan.use_audit and plan.use_telemetry:
        return plan.limit + plan.offset, 0
    return plan.limit, plan.offset


# meiva:note
# The query parameters of one `GET /api/agents/{id}/events` request.
@dataclass(frozen=True)
class AgentEventsRequest:
    agent_id: int
    event: str | None
    level: str | None
    from_: datetime | None
    to: datetime | None
    limit: int | None
    offset: int


# meiva:note
# The telemetry read behind `GET /api/agents/{id}/events`.
@dataclass(frozen=True)
class AgentEventsQuery:
    agent_id: int
    categories: list[str]
    event_names: list[str] | None
    level: str | None
    from_: datetime
    to: datetime | None
    limit: int
    offset: int


# meiva:note
# One agent's history: telemetry and log rows only (audit rows never leak into the feed).
#
# `level` is an exact match, not a minimum. An absent `from` defaults to the last 24 hours.
def plan_agent_events(
    request: AgentEventsRequest, *, now: datetime, default_limit: int
) -> AgentEventsQuery:
    return AgentEventsQuery(
        agent_id=request.agent_id,
        categories=["telemetry", "log"],
        event_names=[request.event] if request.event is not None else None,
        level=request.level,
        from_=request.from_
        if request.from_ is not None
        else now - timedelta(hours=_DEFAULT_WINDOW_HOURS),
        to=request.to,
        limit=request.limit if request.limit is not None else default_limit,
        offset=request.offset,
    )
