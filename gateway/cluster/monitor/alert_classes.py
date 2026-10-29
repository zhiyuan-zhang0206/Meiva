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

from datetime import datetime
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request
from psycopg.rows import dict_row

from gateway.cluster.monitor.stats_events import set_statement_timeout
from gateway.cluster.schemas import (
    AlertClassesResponse,
    AlertClassRow,
    AlertClassSample,
    AlertClassSamples,
    AlertLevel,
)
from gateway.http.config import GatewayConfig, gateway_clock, gateway_cluster
from gateway.schemas.stats import StatsWindowHours, window_delta
from services.upkeep.events_maintenance import resolution

router = APIRouter()


# meiva:note
# Every warning/error class over `(start, end]` for the home cluster, most frequent first.
def read_alert_classes(
    conn: psycopg.Connection[Any], *, cluster: str, start: datetime, end: datetime
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
            )
        )
    return rows


@router.get(
    "/api/stats/alert-classes",
    description="The selected window's warning/error classes, most frequent first.\n\nActive and dismissed classes come together (`dismissal_id` tells them apart) so the console\ncan list the dismissed ones apart and reopen them. Capped at `GatewayConfig.alert_classes_limit` rows;\n`total_classes` / `total_events` are uncapped. One connection, an 8-second statement\ntimeout (a timeout is a retriable 503).",
)
def get_alert_classes(
    request: Request,
    hours: Annotated[StatsWindowHours, Query()] = StatsWindowHours.H24,
) -> AlertClassesResponse:
    now = gateway_clock(request).now()
    config: GatewayConfig = request.app.state.gateway_config
    try:
        with request.app.state.db_pool.connection() as conn:
            set_statement_timeout(conn, config.stats_statement_timeout_ms)
            classes = read_alert_classes(
                conn,
                cluster=gateway_cluster(request),
                start=now - window_delta(hours),
                end=now,
            )
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(status_code=503, detail="alert classes read timed out; retry") from exc
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
    config: GatewayConfig = request.app.state.gateway_config
    try:
        with (
            request.app.state.db_pool.connection() as conn,
            conn.cursor(row_factory=dict_row) as cur,
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
    return AlertClassSamples(samples=[AlertClassSample(**row) for row in rows])
