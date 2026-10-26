"""Unified event stream query — `GET /api/events`.

The programmatic query surface over the unified event stream (audit /
telemetry / log). One schema and one correlation key (`trace_id`). Audit rows
are read from `audit_events` (`base/events/reads/audit_rows.py`) and telemetry and
log rows from `telemetry_events` (`gateway/events/telemetry_rows.py`), both in
Postgres and both permanent. A request that spans both is answered by one
merge, newest first. `telemetry_events` stores only the events a reader queries by name and
every event at warning level or above, so a trace's full chain is in Loki (84 hours) or the
JSONL event mirror.

Filters compose (AND): `category` / `event_name` / `tier` / `agent_id` /
`trace_id` / `machine` / `level`, plus a
time window given either as `from`/`to`
(ISO-8601, inclusive) or as `hours` (the last N hours — shorthand for
`from = now - hours`; the two forms are mutually exclusive). `level` is an
exact match (case-insensitive), the same reading as the per-agent events
query. Pagination is `limit` + `offset`; `meta.has_more` (from the list
fetch's +1 lookahead) tells the client whether another page exists, and
`meta.total` — the exact filtered row count before paging — is opt-in via
`with_total=1` (it costs a full-window count aggregation, which a page flip
does not need).

Two hard contract rules keep every query bounded and unambiguous:
  - a lower bound is always in effect — absent both `from` and `hours`,
    `from = now - 24h` is assumed (the old PG scan needed the partition
    prune; it keeps the count/list fetch cheap — the API never
    runs an unbounded window);
  - `from` / `to` must carry a timezone offset — a naive timestamp would be
    interpreted in the server's local timezone, silently shifting the
    window; such input is rejected with 422 instead.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from base.events.contract import tier_for
from base.events.reads import audit_rows
from gateway.agents.eval_guard import deny_isolated_result_read
from gateway.events import telemetry_rows
from gateway.events.events_plan import (
    EventsPlan,
    EventsRequest,
    InvalidEventsQueryError,
    page_depth,
    plan_events,
)
from gateway.events.schemas import EventRow, EventsMeta, EventsResponse
from gateway.http.config import GatewayConfig, gateway_clock, gateway_config

router = APIRouter()

# Longest window a request may name; a protective constant, evaluated at import for the `hours` Query
# bound — not configuration (task #3696 exception inventory: KEEP).
_MAX_HOURS = 24 * 365


@contextmanager
def _read_connection(request: Request) -> Generator[psycopg.Connection]:
    """One pooled connection for an events read, bounded by a statement timeout."""
    config: GatewayConfig = request.app.state.gateway_config
    with request.app.state.db_pool.connection() as conn:
        conn.execute(f"SET LOCAL statement_timeout = {config.events_read_timeout_ms}")
        yield conn


def _count(request: Request, plan: EventsPlan) -> int:
    """Exact filtered row count across the tables the request reads."""
    total = 0
    with _read_connection(request) as conn:
        if plan.use_audit:
            total += audit_rows.count_events(conn, **asdict(plan.filters))
        if plan.use_telemetry:
            total += telemetry_rows.count_events(
                conn, categories=plan.telemetry_categories, **asdict(plan.filters)
            )
    return total


# meiva:note
# One newest-first page, and whether another exists.
#
# One table pages directly. Two tables each give their newest `offset + limit`
# rows (`page_depth`), which are merged and then sliced.
def _read_page(request: Request, plan: EventsPlan) -> tuple[list[dict[str, Any]], bool]:
    both = plan.use_audit and plan.use_telemetry
    depth, page_offset = page_depth(plan)
    rows: list[dict[str, Any]] = []
    has_more = False
    with _read_connection(request) as conn:
        if plan.use_audit:
            page, more = audit_rows.query_events(
                conn, limit=depth, offset=page_offset, **asdict(plan.filters)
            )
            rows += page
            has_more = has_more or more
        if plan.use_telemetry:
            page, more = telemetry_rows.query_events(
                conn,
                categories=plan.telemetry_categories,
                limit=depth,
                offset=page_offset,
                **asdict(plan.filters),
            )
            rows += page
            has_more = has_more or more
    if both:
        rows.sort(key=lambda row: (row["ts"], row["id"]), reverse=True)
        has_more = has_more or len(rows) > plan.limit + plan.offset
        rows = rows[plan.offset : plan.offset + plan.limit]
    return rows, has_more


@router.get("/api/events", dependencies=[Depends(deny_isolated_result_read)])
def get_events(
    request: Request,
    category: Annotated[str | None, Query()] = None,
    event_name: Annotated[str | None, Query()] = None,
    agent_id: Annotated[int | None, Query()] = None,
    trace_id: Annotated[str | None, Query()] = None,
    impersonation_session: Annotated[str | None, Query()] = None,
    machine: Annotated[str | None, Query()] = None,
    level: Annotated[str | None, Query()] = None,
    tier: Annotated[str | None, Query()] = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    hours: Annotated[float | None, Query(gt=0, le=_MAX_HOURS)] = None,
    # `limit`'s range and `offset`'s ceiling stay protective constants (import-
    # time Query bounds); the default *window* is display.events_default_limit.
    limit: Annotated[int | None, Query(ge=1, le=1000)] = None,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
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
      - `trace_id=<hex>`: the events of one turn that share the id and that
        Postgres stores. `telemetry_events` keeps only the events a reader queries
        by name (`persist=True` in the registry) and every event at warning level or
        above, so this is not the whole chain: read the rest from Loki (84-hour
        window) or the JSONL event mirror.
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
        100 out of the box — cap 1000) / `offset` (cap 10,000): offset
        paging with stable ordering across same-`ts` rows. The cap bounds the
        rows one read materializes (`limit + offset + 1`).
      - `with_total=1`: also compute the exact filtered row count
        (`meta.total`) — one extra full-window count, so it is opt-in; without it `meta.total` is null.

    Response: `meta` (opt-in exact filtered `total`, effective
    `window_from`/`window_to`, `limit`/`offset`, `has_more` from the list
    fetch's +1 lookahead) + `items` (the unified `EventRow` shape).
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
                impersonation_session=impersonation_session,
                machine=machine,
                level=level,
                tier=tier,
                from_=from_,
                to=to,
                hours=hours,
                limit=limit,
                offset=offset,
                with_total=with_total,
            ),
            now=now,
            default_limit=gateway_config(request).display.events_default_limit,
        )
    except InvalidEventsQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        total = _count(request, plan) if plan.with_total else None
        rows, has_more = _read_page(request, plan)
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(status_code=503, detail="events read timed out") from exc

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
        offset=plan.offset,
        has_more=has_more,
        generated_at=now.isoformat(),
    )
    return EventsResponse(meta=meta, items=items)
