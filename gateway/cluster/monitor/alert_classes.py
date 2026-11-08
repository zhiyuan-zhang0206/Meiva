# meiva:note
# Warning/error classes of the stats window, one row per class — the sidebar card's detail.
#
# The dashboard card counts active classes instead of raw events, the way an error tracker
# groups an event stream: `GET /api/stats/alert-classes` lists each
# `(level, event_name, source, process)` class of the selected window with its count, first and
# last occurrence and the dismissal that cancels it, and `.../samples` opens one class to its
# newest events. Dismissing and reopening go through `/api/event-resolutions`
# (`gateway/events/resolutions.py`); the dismissal match is the events-maintenance daemon's
# (`services.upkeep.events_maintenance.resolution.matching_dismissal`), so a class reads as dismissed
# here exactly when the unresolved gauges stop counting it.

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Annotated, NamedTuple

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request
from psycopg.rows import class_row

from gateway.cluster.monitor.stats_events import set_statement_timeout
from gateway.cluster.schemas import (
    AlertClassesResponse,
    AlertClassRow,
    AlertClassSample,
    AlertClassSamples,
    AlertLevel,
)
from gateway.http.config import GatewayConfig, gateway_clock, gateway_cluster, gateway_state
from gateway.schemas.stats import StatsWindowHours, window_delta
from services.upkeep.events_maintenance import resolution

router = APIRouter()


# meiva:note
# Every warning/error class over the window, each with why it is resolved (or None). `end` is the
# read's "now": the quiet rule asks whether the class recorded anything over the whole window
# ending here, so a class whose last event is older than `quiet_window` is resolved even though
# it is still inside the selected window (task #6540).
# meiva:link docs/decisions/observability/events/2026-11-08-quiet-auto-resolution.md
def read_alert_classes(
    conn: psycopg.Connection,
    *,
    cluster: str,
    start: datetime,
    end: datetime,
    quiet_window: timedelta,
) -> list[AlertClassRow]:
    active = resolution.active_dismissals(conn)
    rows: list[AlertClassRow] = []
    for found in resolution.alert_classes(conn, start=start, end=end, cluster=cluster):
        dismissal = resolution.matching_dismissal(
            resolution.EventClass(
                category=found.category,
                level=found.level,
                event_name=found.event_name,
                source=found.source,
                process=found.process,
            ),
            active,
        )
        rows.append(
            AlertClassRow(
                level=found.level,  # pyright: ignore[reportArgumentType]
                event_name=found.event_name,
                source=found.source,
                process=found.process,
                category=found.category,  # pyright: ignore[reportArgumentType]
                count=found.count,
                first_seen=found.first_seen,
                last_seen=found.last_seen,
                dismissal_id=dismissal.id if dismissal is not None else None,
                resolved_by=resolution.resolution_state(
                    dismissal,
                    last_seen=found.last_seen,
                    at=end,
                    quiet_window=quiet_window,
                ),
            )
        )
    return rows


# meiva:note
# One window's classes, counted the way the dashboard reports them.
class AlertClassTotals(NamedTuple):
    warnings: int
    errors: int
    warnings_resolved: int
    errors_resolved: int
    classes_unresolved: int
    classes_resolved: int


# meiva:note
# What the dashboard's warning/error fields mean, in one place: event counts of the classes
# that still need attention, the same counts for the resolved ones, and how many classes sit
# on each side. Pure over the rows -- `read_alert_classes` already folded the dismissal and
# the quiet window into `resolved_by`, so this only splits on it (critical folds into errors).
# meiva:link gateway/cluster/docs/alert-classes-api.meiva.okf.md
def alert_class_totals(rows: Sequence[AlertClassRow]) -> AlertClassTotals:
    warnings = [row for row in rows if row.level == "warning"]
    errors = [row for row in rows if row.level != "warning"]
    warnings_unresolved, warnings_resolved = _event_counts(warnings)
    errors_unresolved, errors_resolved = _event_counts(errors)
    classes_unresolved = sum(1 for row in rows if row.resolved_by is None)
    return AlertClassTotals(
        warnings=warnings_unresolved,
        errors=errors_unresolved,
        warnings_resolved=warnings_resolved,
        errors_resolved=errors_resolved,
        classes_unresolved=classes_unresolved,
        classes_resolved=len(rows) - classes_unresolved,
    )


# meiva:note
# The events these rows hold that still need attention, and the rest.
def _event_counts(rows: Sequence[AlertClassRow]) -> tuple[int, int]:
    unresolved = sum(row.count for row in rows if row.resolved_by is None)
    return unresolved, sum(row.count for row in rows) - unresolved


@router.get(
    "/api/stats/alert-classes",
    description="The selected window's warning/error classes, most frequent first.\n\nOnly unresolved classes by default: a class is resolved when an active dismissal cancels it or when\n`GatewayConfig.alert_class_quiet_seconds` passed with nothing recorded (`resolved_by` says which).\n`include_resolved=true` lists the resolved ones too — the history view. The listed rows are capped at\n`GatewayConfig.alert_classes_limit`; `total_classes` / `total_events` count the whole returned set.\nOne connection, an 8-second statement timeout (a timeout is a retriable 503).",
)
def get_alert_classes(
    request: Request,
    hours: Annotated[StatsWindowHours, Query()] = StatsWindowHours.H24,
    *,
    include_resolved: Annotated[bool, Query()] = False,
) -> AlertClassesResponse:
    now = gateway_clock(request).now()
    config: GatewayConfig = gateway_state(request).gateway_config
    try:
        with gateway_state(request).db_pool.connection() as conn:
            set_statement_timeout(conn, config.stats_statement_timeout_ms)
            classes = read_alert_classes(
                conn,
                cluster=gateway_cluster(request),
                start=now - window_delta(hours),
                end=now,
                quiet_window=timedelta(seconds=config.alert_class_quiet_seconds),
            )
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(status_code=503, detail="alert classes read timed out; retry") from exc
    if not include_resolved:
        classes = [row for row in classes if row.resolved_by is None]
    return AlertClassesResponse(
        window_hours=hours,
        classes=classes[: config.alert_classes_limit],
        total_classes=len(classes),
        total_events=sum(row.count for row in classes),
        as_of=gateway_clock(request).now(),
    )


@router.get(
    "/api/stats/alert-classes/samples",
    description="The newest `GatewayConfig.alert_samples_limit` events of one class in the window, with their message and context.\n\n`process` matches exactly (an empty value is the empty-process class). The literal level\npredicate lets the partial index on warning/error/critical rows serve the newest-first scan.",
)
def get_alert_class_samples(
    request: Request,
    level: Annotated[AlertLevel, Query()],
    event_name: Annotated[str, Query(min_length=1, max_length=255)],
    source: Annotated[str, Query(min_length=1, max_length=255)],
    process: Annotated[str, Query(max_length=255)] = "",
    hours: Annotated[StatsWindowHours, Query()] = StatsWindowHours.H24,
) -> AlertClassSamples:
    now = gateway_clock(request).now()
    config: GatewayConfig = gateway_state(request).gateway_config
    try:
        with (
            gateway_state(request).db_pool.connection() as conn,
            conn.cursor(row_factory=class_row(AlertClassSample)) as cur,
        ):
            set_statement_timeout(cur, config.stats_statement_timeout_ms)
            cur.execute(
                """
                SELECT ts, agent_id, machine, trace_id, attributes->>'msg' AS message, attributes
                FROM telemetry_events
                WHERE level IN ('warning', 'error', 'critical')
                  AND level = %s AND event_name = %s AND source = %s AND process = %s
                  AND (cluster = %s OR cluster = '')
                  AND ts > %s AND ts <= %s
                ORDER BY ts DESC
                LIMIT %s
                """,
                (
                    level,
                    event_name,
                    source,
                    process,
                    gateway_cluster(request),
                    now - window_delta(hours),
                    now,
                    config.alert_samples_limit,
                ),
            )
            rows = cur.fetchall()
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(status_code=503, detail="alert samples read timed out; retry") from exc
    return AlertClassSamples(samples=rows)
