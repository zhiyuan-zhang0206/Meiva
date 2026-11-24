"""Unified event stream query — `GET /api/events`.

The programmatic query surface over the unified event stream (audit / telemetry /
log). One schema and one correlation key (`trace_id`). Audit rows are read from
`audit_events` (`base/events/reads/audit_rows.py`, Postgres, permanent). Telemetry and log
rows are read from Loki (`gateway/events/loki_rows.py`), which holds the whole stream for seven
days; an older telemetry or log event is gone. A request that spans both is answered by one
merge, newest first.

Filters compose (AND): `category` / `event_name` / `tier` / `agent_id` /
`trace_id` / `machine` / `level`, plus a
time window given either as `from`/`to`
(ISO-8601, inclusive) or as `hours` (the last N hours — shorthand for
`from = now - hours`; the two forms are mutually exclusive). `level` is an
exact match (case-insensitive), the same reading as the per-agent events
query. Paging is by time: a page is the newest `limit` events of the window, `meta.has_more`
(from the list fetch's +1 lookahead) says whether older ones remain, and `meta.next_to` is the
`to` that asks for them. `meta.total` — the filtered row count before paging — is opt-in via
`with_total=1` (it costs a full-window count, which a page flip does not need).

Two hard contract rules keep every query bounded and unambiguous:
  - a lower bound is always in effect — absent both `from` and `hours`,
    `from = now - 24h` is assumed (the API never runs an unbounded window);
  - `from` / `to` must carry a timezone offset — a naive timestamp would be
    interpreted in the server's local timezone, silently shifting the
    window; such input is rejected with 422 instead.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated

import httpx
import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from base.events.contract import EventGroupBy, tier_for
from base.events.reads import EventRecord, audit_rows
from gateway.agents.eval_guard import deny_isolated_result_read
from gateway.events import loki_rows
from gateway.events.events_plan import (
    EventsPlan,
    EventsRequest,
    InvalidEventsQueryError,
    next_page_to,
    plan_events,
    telemetry_filters,
)
from gateway.events.schemas import (
    EventCountGroup,
    EventCountsMeta,
    EventCountsResponse,
    EventRow,
    EventsMeta,
    EventsResponse,
)
from gateway.http.config import (
    GatewayConfig,
    gateway_clock,
    gateway_config,
    gateway_state,
    state_as,
)
from gateway.lgtm.backend_failure import raise_backend_unavailable

router = APIRouter()

# Longest window a request may name; a protective constant, evaluated at import for the `hours` Query
# bound — not configuration (task #3696 exception inventory: KEEP).
_MAX_HOURS = 24 * 365


@contextmanager
def _read_connection(request: Request) -> Generator[psycopg.Connection]:
    """One pooled connection for an events read, bounded by a statement timeout."""
    config: GatewayConfig = gateway_state(request).gateway_config
    with gateway_state(request).db_pool.connection() as conn:
        conn.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(config.events_read_timeout_ms),),
        )
        yield conn


def _count(request: Request, plan: EventsPlan, now: datetime) -> int:
    """Filtered row count across the stores the request reads (Loki's counts to the millisecond)."""
    total = 0
    f = plan.filters
    if plan.use_audit:
        with _read_connection(request) as conn:
            total += audit_rows.count_events(conn, **f.kwargs())
    if plan.use_telemetry:
        total += loki_rows.count_events(
            state_as(request, loki_rows.LokiAppState).loki_client,
            telemetry_filters(plan),
            from_=f.from_,
            to=f.to,
            now=now,
        )
    return total


# meiva:note
# One newest-first page, and whether older rows remain in the window.
#
# Each store gives its newest `limit` rows (one more, to see whether others remain); two stores'
# rows are merged and cut to the page.
def _read_page(request: Request, plan: EventsPlan, now: datetime) -> tuple[list[EventRecord], bool]:
    f = plan.filters
    rows: list[EventRecord] = []
    has_more = False
    if plan.use_audit:
        with _read_connection(request) as conn:
            page, more = audit_rows.query_events(conn, limit=plan.limit, **f.kwargs())
        rows += page
        has_more = has_more or more
    if plan.use_telemetry:
        page, more = loki_rows.query_events(
            state_as(request, loki_rows.LokiAppState).loki_client,
            telemetry_filters(plan),
            from_=f.from_,
            to=f.to,
            now=now,
            limit=plan.limit,
        )
        rows += page
        has_more = has_more or more
    if plan.use_audit and plan.use_telemetry:
        rows.sort(key=lambda row: (row["ts"], row["id"]), reverse=True)
        has_more = has_more or len(rows) > plan.limit
        rows = rows[: plan.limit]
    return rows, has_more


@router.get("/api/events", dependencies=[Depends(deny_isolated_result_read)])
def get_events(
    request: Request,
    category: Annotated[str | None, Query()] = None,
    event_name: Annotated[str | None, Query()] = None,
    agent_id: Annotated[int | None, Query()] = None,
    trace_id: Annotated[str | None, Query()] = None,
    machine: Annotated[str | None, Query()] = None,
    level: Annotated[str | None, Query()] = None,
    tier: Annotated[str | None, Query()] = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    hours: Annotated[float | None, Query(gt=0, le=_MAX_HOURS)] = None,
    # `limit`'s range stays a protective constant (an import-time Query bound); the default
    # *window* is display.events_default_limit.
    limit: Annotated[int | None, Query(ge=1, le=1000)] = None,
    with_total: Annotated[bool, Query()] = False,  # noqa: FBT002 — FastAPI query param
) -> EventsResponse:
    """Slice of the unified event stream — every event (audit / telemetry /
    log) through one query surface, the programmatic read side of the
    unified emitter. Newest-first (`ts DESC`, `id DESC` tiebreak).

    Filters compose (AND):
      - `category=<audit|telemetry|log>`: retention/alerting class (unknown
        value 422s).
      - `event_name=<name>`: exact event name, e.g. `llm_usage` /
        `turn_end` / `spawn` / `send_message`.
      - `agent_id=<n>`: events belonging to that agent; service-level events
        (NULL) are excluded when set.
      - `trace_id=<hex>`: the events of one turn that share the id: its whole chain
        for the telemetry and log events of the last seven days (Loki), and its audit
        events without that limit.
      - `machine=<name>`: the host dimension.
      - `level=<debug|info|warning|error|critical>`: exact match,
        case-insensitive (unknown value 422s).
      - `tier=<business|anomaly|observation|noise>[,...]`: comma-separated
        display tiers, ORed within the list and ANDed with every other filter.
        The tier predicate is applied before pagination so `meta.total` and
        page boundaries stay exact.
      - `from=<ISO-8601>` / `to=<ISO-8601>`: inclusive time window
        (`ts >= from AND ts <= to`); either side may be omitted. Values
        MUST carry a timezone offset (`Z` or `+hh:mm`) — a naive timestamp
        is interpreted in the server's local timezone, so it 422s instead
        of silently shifting the window.
      - `hours=<n>`: alternative window — the last N hours
        (`from = now - hours`). Mutually exclusive with `from`.
      - Default window: when neither `from` nor `hours` is given, the last
        24 hours are assumed (`from = now - 24h`) — an unbounded query
        would scan the whole record, so the API never runs one. `meta.window_from`
        always echoes the effective lower bound.
      - `limit` (configured default window — ``display.events_default_limit``,
        100 out of the box — cap 1000): the page size.
      - Paging is by time: when `meta.has_more` is true, repeat the request with
        `to=meta.next_to` for the next older page. A row that shares the
        oldest row's microsecond and did not fit the page is skipped.
      - `with_total=1`: also compute the filtered row count
        (`meta.total`; the Loki part is counted to the millisecond) — one extra
        full-window count, so it is opt-in; without it `meta.total` is null.

    Telemetry and log events older than seven days are not available (Loki's
    retention); a window that reaches past it returns what remains.

    Response: `meta` (opt-in filtered `total`, effective
    `window_from`/`window_to`, `limit`, `has_more` from the list fetch's +1
    lookahead, and `next_to`) + `items` (the unified `EventRow` shape).
    `window_from` is always set — the default 24h lower bound when the
    request named none. An empty window returns `items: []`.
    """
    now = gateway_clock(request).now()
    try:
        plan = plan_events(
            EventsRequest(
                category=category,
                event_name=event_name,
                agent_id=agent_id,
                trace_id=trace_id,
                machine=machine,
                level=level,
                tier=tier,
                from_=from_,
                to=to,
                hours=hours,
                limit=limit,
                with_total=with_total,
            ),
            now=now,
            default_limit=gateway_config(request).display.events_default_limit,
        )
    except InvalidEventsQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        total = _count(request, plan, now) if plan.with_total else None
        rows, has_more = _read_page(request, plan, now)
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(status_code=503, detail="events read timed out") from exc
    except httpx.HTTPError as exc:
        raise_backend_unavailable(exc)

    items = [
        EventRow(
            id=row["id"],
            line_sha256=row["line_sha256"],
            ts=row["ts"],
            trace_id=row["trace_id"],
            span_id=row["span_id"],
            agent_id=row["agent_id"],
            machine=row["machine"],
            process=row["process"],
            category=row["category"],
            event_name=row["event_name"],
            tier=tier_for(row["event_name"], row["category"], row["level"]),
            level=row["level"],
            source=row["source"],
            target_agent_id=row["target_agent_id"],
            attributes=row["attributes"],
        )
        for row in rows
    ]
    meta = EventsMeta(
        total=total,
        window_from=plan.filters.from_,
        window_to=plan.filters.to,
        limit=plan.limit,
        has_more=has_more,
        next_to=next_page_to(rows[-1]["ts"] if rows else None, has_more=has_more),
        generated_at=now.isoformat(),
    )
    return EventsResponse(meta=meta, items=items)


# meiva:note
# How many distinct values one store may report for the grouped dimension before the request is
# refused: the groups of both stores are merged before the `limit` cut, so a store cannot be
# cut early without under-counting a value the other store also holds.
_MAX_GROUPS = 1000


# meiva:note
# Per-value event counts summed across the stores the request reads.
def _merged_groups(
    request: Request, plan: EventsPlan, by: EventGroupBy, now: datetime
) -> dict[str | None, int]:
    f = plan.filters
    merged: dict[str | None, int] = {}
    pages: list[list[tuple[str | None, int]]] = []
    if plan.use_audit:
        with _read_connection(request) as conn:
            pages.append(
                audit_rows.count_event_groups(conn, by=by, limit=_MAX_GROUPS + 1, **f.kwargs())
            )
    if plan.use_telemetry:
        pages.append(
            loki_rows.count_event_groups(
                state_as(request, loki_rows.LokiAppState).loki_client,
                telemetry_filters(plan),
                by=by,
                from_=f.from_,
                to=f.to,
                now=now,
                limit=_MAX_GROUPS + 1,
            )
        )
    for page in pages:
        if len(page) > _MAX_GROUPS:
            raise HTTPException(
                status_code=422,
                detail=f"more than {_MAX_GROUPS} distinct {by} values in the window; "
                "narrow the window or add a filter",
            )
        for key, count in page:
            merged[key] = merged.get(key, 0) + count
    return merged


@router.get(
    "/api/events/counts",
    dependencies=[Depends(deny_isolated_result_read)],
    description="Events of the unified stream counted per value of one dimension (`by`: `event_name`,\n`machine`, `level`, `agent_id` or `category`), largest group first, ties by key.\n\nThe filters and the window are `GET /api/events`' (same names, same 24-hour default lower\nbound, same 422 on a bad value); the counts are computed by the stores (SQL for audit, LogQL for\ntelemetry and log), so the answer is a few rows however many events match. `limit` (default: the configured events page, cap 1000) bounds\nthe groups returned; `meta.total` counts every event of the window and `meta.truncated`\nsays more groups exist. A window with more than 1000 distinct values is refused with 422.",
)
def get_event_counts(
    request: Request,
    by: Annotated[EventGroupBy, Query()],
    category: Annotated[str | None, Query()] = None,
    event_name: Annotated[str | None, Query()] = None,
    agent_id: Annotated[int | None, Query()] = None,
    trace_id: Annotated[str | None, Query()] = None,
    machine: Annotated[str | None, Query()] = None,
    level: Annotated[str | None, Query()] = None,
    tier: Annotated[str | None, Query()] = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    hours: Annotated[float | None, Query(gt=0, le=_MAX_HOURS)] = None,
    limit: Annotated[int | None, Query(ge=1, le=1000)] = None,
) -> EventCountsResponse:
    now = gateway_clock(request).now()
    default_limit = gateway_config(request).display.events_default_limit
    try:
        plan = plan_events(
            EventsRequest(
                category=category,
                event_name=event_name,
                agent_id=agent_id,
                trace_id=trace_id,
                machine=machine,
                level=level,
                tier=tier,
                from_=from_,
                to=to,
                hours=hours,
                limit=None,
                with_total=False,
            ),
            now=now,
            default_limit=default_limit,
        )
    except InvalidEventsQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        merged = _merged_groups(request, plan, by, now)
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(status_code=503, detail="events read timed out") from exc
    except httpx.HTTPError as exc:
        raise_backend_unavailable(exc)
    shown = limit if limit is not None else default_limit
    ordered = sorted(merged.items(), key=lambda item: (-item[1], item[0] is None, item[0] or ""))
    return EventCountsResponse(
        meta=EventCountsMeta(
            by=by,
            window_from=plan.filters.from_,
            window_to=plan.filters.to,
            limit=shown,
            total=sum(merged.values()),
            truncated=len(ordered) > shown,
            generated_at=now.isoformat(),
        ),
        groups=[EventCountGroup(key=key, count=count) for key, count in ordered[:shown]],
    )
