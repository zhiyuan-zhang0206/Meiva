# meiva:note
# Sentry-style error diagnostics grouped from `telemetry_events`, decoupled from live Grafana alerts.
#
# Admission is strict: only `error`/`critical` rows (excluding declared `tier="noise"` telemetry
# other than bare `log`) and `warning` rows whose `event_name` is explicitly declared
# `tier="anomaly"` in `base/events/declarations/`. Bare `log` warnings and `tier="noise"` warnings
# are excluded.
#
# Archiving is automatic and release-aware: when the running gateway knows its loaded commit SHA
# (`deployed_sha`), classes with no occurrences on that SHA are archived (`archived_by="deployed_sha"`),
# while occurrences on the current SHA stay active (and marked `regressed=True` when the same class
# also appeared on an earlier SHA in the window) until `GatewayConfig.diagnostic_quiet_seconds`
# elapses without a recurrence (`archived_by="quiet"`). There is no permanent manual dismissal.

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request
from psycopg.rows import class_row

from base.db.rows import query_all
from base.events.contract import EVENTS
from gateway.cluster.monitor.stats_events import set_statement_timeout
from gateway.cluster.schemas import (
    DiagnosticArchiveReason,
    DiagnosticClassRow,
    DiagnosticLevel,
    DiagnosticSample,
    DiagnosticSamples,
    DiagnosticsResponse,
    StatsDiagnostics,
)
from gateway.http.config import GatewayConfig, gateway_clock, gateway_cluster, gateway_state
from gateway.schemas.stats import StatsWindowHours, window_delta

router = APIRouter()

# meiva:note
# Derived once from the single event registry: `warning` rows require an explicit `tier="anomaly"`
# declaration, while `error`/`critical` rows drop non-log `tier="noise"` events.
ANOMALY_EVENT_NAMES: tuple[str, ...] = tuple(
    sorted(name for name, spec in EVENTS.items() if spec.tier == "anomaly")
)
EXCLUDED_NOISE_EVENT_NAMES: tuple[str, ...] = tuple(
    sorted(name for name, spec in EVENTS.items() if spec.tier == "noise" and name != "log")
)


@dataclass(frozen=True)
class _DiagnosticClassDbRow:
    level: str
    event_name: str
    source: str
    process: str
    category: str
    count: int
    current_sha_count: int
    first_seen: datetime
    last_seen: datetime
    current_sha_last_seen: datetime | None
    last_deployed_sha: str | None


# meiva:note
# Pure archive verdict for one diagnostic class: release cutover outranks the quiet fallback.
# When `current_sha` is unknown (non-git checkout), fall back to the quiet window alone.
def archive_reason(
    *,
    current_sha: str | None,
    current_sha_last_seen: datetime | None,
    last_seen: datetime,
    at: datetime,
    quiet_window: timedelta,
) -> DiagnosticArchiveReason | None:
    if current_sha:
        if current_sha_last_seen is None:
            return "deployed_sha"
        if at - current_sha_last_seen >= quiet_window:
            return "quiet"
        return None
    if at - last_seen >= quiet_window:
        return "quiet"
    return None


# meiva:note
# Aggregate one window's diagnostic classes from `telemetry_events` and tag each class's
# release/quiet archive status against `current_sha` and `end`.
def read_diagnostic_classes(
    conn: psycopg.Connection,
    *,
    cluster: str,
    start: datetime,
    end: datetime,
    current_sha: str | None,
    quiet_window: timedelta,
) -> list[DiagnosticClassRow]:
    sha = current_sha or ""
    found_rows = query_all(
        conn,
        _DiagnosticClassDbRow,
        """
        SELECT level, event_name, source, process, max(category) AS category,
               count(*) AS count,
               count(*) FILTER (WHERE %s != '' AND deployed_sha = %s) AS current_sha_count,
               min(ts) AS first_seen,
               max(ts) AS last_seen,
               max(ts) FILTER (WHERE %s != '' AND deployed_sha = %s) AS current_sha_last_seen,
               NULLIF((ARRAY_AGG(deployed_sha ORDER BY ts DESC))[1], '') AS last_deployed_sha
        FROM telemetry_events
        WHERE level IN ('warning', 'error', 'critical')
          AND ts > %s AND ts <= %s
          AND (cluster = %s OR cluster = '')
          AND (
              (level IN ('error', 'critical') AND event_name != ALL(%s))
              OR (level = 'warning' AND event_name = ANY(%s))
          )
        GROUP BY level, event_name, source, process
        ORDER BY count(*) DESC, event_name, source, process, level
        """,
        (
            sha,
            sha,
            sha,
            sha,
            start,
            end,
            cluster,
            list(EXCLUDED_NOISE_EVENT_NAMES),
            list(ANOMALY_EVENT_NAMES),
        ),
    )
    rows: list[DiagnosticClassRow] = []
    for found in found_rows:
        effective_current_count = found.current_sha_count if sha else found.count
        regressed = bool(
            sha and found.current_sha_count > 0 and found.count > found.current_sha_count
        )
        rows.append(
            DiagnosticClassRow(
                level=found.level,  # pyright: ignore[reportArgumentType]
                event_name=found.event_name,
                source=found.source,
                process=found.process,
                category=found.category,  # pyright: ignore[reportArgumentType]
                count=found.count,
                current_sha_count=effective_current_count,
                first_seen=found.first_seen,
                last_seen=found.last_seen,
                last_deployed_sha=found.last_deployed_sha,
                regressed=regressed,
                archived_by=archive_reason(
                    current_sha=current_sha,
                    current_sha_last_seen=found.current_sha_last_seen,
                    last_seen=found.last_seen,
                    at=end,
                    quiet_window=quiet_window,
                ),
            )
        )
    return rows


# meiva:note
# Roll up diagnostic classes into the dashboard's `diagnostics` summary. Active classes contribute
# their current-release events to `active_events` (`warnings` / `errors`, with critical folded into
# errors); prior-release and archived events stay counted in `archived_events`.
def diagnostic_totals(
    rows: Sequence[DiagnosticClassRow], *, deployed_sha: str | None
) -> StatsDiagnostics:
    active_classes = 0
    active_events = 0
    archived_events = 0
    warnings = 0
    errors = 0
    for row in rows:
        if row.archived_by is None:
            active_classes += 1
            active_count = row.current_sha_count if deployed_sha else row.count
            active_events += active_count
            archived_events += row.count - active_count
            if row.level == "warning":
                warnings += active_count
            else:
                errors += active_count
        else:
            archived_events += row.count
    return StatsDiagnostics(
        active_classes=active_classes,
        archived_classes=len(rows) - active_classes,
        active_events=active_events,
        archived_events=archived_events,
        errors=errors,
        warnings=warnings,
        deployed_sha=deployed_sha,
    )


@router.get(
    "/api/stats/diagnostics",
    description=(
        "The selected window's diagnostic error/anomaly classes, most frequent first.\n\n"
        "Only active classes by default: a class is automatically archived when it has not\n"
        "occurred on the currently deployed commit SHA (`archived_by='deployed_sha'`) or when\n"
        "`GatewayConfig.diagnostic_quiet_seconds` passed with nothing recorded (`archived_by='quiet'`).\n"
        "`include_archived=true` lists archived classes too. Capped at `GatewayConfig.diagnostics_limit`."
    ),
)
def get_diagnostics(
    request: Request,
    hours: Annotated[StatsWindowHours, Query()] = StatsWindowHours.H24,
    *,
    include_archived: Annotated[bool, Query()] = False,
) -> DiagnosticsResponse:
    now = gateway_clock(request).now()
    config: GatewayConfig = gateway_state(request).gateway_config
    current_sha = gateway_state(request).process_image.sha
    try:
        with gateway_state(request).db_pool.connection() as conn:
            set_statement_timeout(conn, config.stats_statement_timeout_ms)
            classes = read_diagnostic_classes(
                conn,
                cluster=gateway_cluster(request),
                start=now - window_delta(hours),
                end=now,
                current_sha=current_sha,
                quiet_window=timedelta(seconds=config.diagnostic_quiet_seconds),
            )
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(status_code=503, detail="diagnostics read timed out; retry") from exc
    if not include_archived:
        classes = [row for row in classes if row.archived_by is None]
    return DiagnosticsResponse(
        window_hours=hours,
        deployed_sha=current_sha,
        classes=classes[: config.diagnostics_limit],
        total_classes=len(classes),
        total_events=sum(row.count for row in classes),
        as_of=gateway_clock(request).now(),
    )


@router.get(
    "/api/stats/diagnostics/samples",
    description=(
        "The newest `GatewayConfig.diagnostic_samples_limit` events of one diagnostic class "
        "in the window, with their message, deployed SHA and context."
    ),
)
def get_diagnostic_samples(
    request: Request,
    level: Annotated[DiagnosticLevel, Query()],
    event_name: Annotated[str, Query(min_length=1, max_length=255)],
    source: Annotated[str, Query(min_length=1, max_length=255)],
    process: Annotated[str, Query(max_length=255)] = "",
    hours: Annotated[StatsWindowHours, Query()] = StatsWindowHours.H24,
) -> DiagnosticSamples:
    now = gateway_clock(request).now()
    config: GatewayConfig = gateway_state(request).gateway_config
    try:
        with (
            gateway_state(request).db_pool.connection() as conn,
            conn.cursor(row_factory=class_row(DiagnosticSample)) as cur,
        ):
            set_statement_timeout(cur, config.stats_statement_timeout_ms)
            cur.execute(
                """
                SELECT ts, agent_id, machine, trace_id,
                       NULLIF(deployed_sha, '') AS deployed_sha,
                       attributes->>'msg' AS message, attributes
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
                    config.diagnostic_samples_limit,
                ),
            )
            rows = cur.fetchall()
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(
            status_code=503, detail="diagnostic samples read timed out; retry"
        ) from exc
    return DiagnosticSamples(samples=rows)
