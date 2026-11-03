# meiva:note
# Ops series — the query core behind `GET /api/ops/monitor` (the Insights Ops panel).
#
# Same report shape and the same fixed bucket grid as always (`OPS_GRID_ORIGIN`-aligned; 1h→60s,
# 6h→300s, 24h→1800s, 7d→3600s, zero-filled). Every metric group is computed from the
# `telemetry_events` rows of the window, in one pooled connection, one statement per group:
#
# - **sse** — `sse_drop` counted by payload `kind` (`queue_full` vs everything else; rows without a
#   kind count toward neither) + `event_log_drop`, per bucket.
# - **llm** — from the `llm_usage` rows: calls, tokens, latency sum, exact p50 / p95 / max latency
#   per bucket (and over the whole window), plus the LLM error family events per bucket.
# - **restarts** — `service_started` per bucket, plus a whole-window breakdown of services by
#   `attributes.name` (count and last start).
#
# A bucket is half-open, `[start, start + bucket)`, as `meta.bucket_starts` says; the in-progress
# bucket is partial. Counts and sums are exact; percentiles are exact over the rows of the bucket.

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg

from base.db.rows import query_all
from base.events.contract import (
    LLM_ERROR_FAMILY,
    LLM_USAGE_KEYS,
    OPS_GRID_ORIGIN,
    SERVICE_STARTED_KEYS,
    SSE_DROP_KEYS,
    family_events,
)
from base.telemetry.metrics.event_sql import numeric
from gateway.cluster.schemas import (
    LlmBucket,
    LlmReport,
    LlmTotals,
    OpsMonitorMeta,
    OpsMonitorReport,
    RestartBucket,
    RestartReport,
    RestartTotals,
    ServiceRestartRow,
    SseDropBucket,
    SseReport,
    SseTotals,
)

# meiva:note
# Window -> (seconds, bucket seconds). Bucket count per window is fixed:
# 1h=60, 6h=72, 24h=48, 7d=168 points — enough shape, small payloads.
WINDOWS: dict[str, tuple[int, int]] = {
    "1h": (3600, 60),
    "6h": (21600, 300),
    "24h": (86400, 1800),
    "7d": (604800, 3600),
}

GRID_ORIGIN = OPS_GRID_ORIGIN
_LLM_ERROR_EVENTS = list(family_events(LLM_ERROR_FAMILY))


# meiva:note
# Bucket boundary times covering `[anchor - window, anchor]`, oldest
# first, on the fixed 60s grid. The last bucket is the in-progress one
# (start <= anchor < start + bucket_s), so the panel's live-most point is
# always present; the series arrays are indexed by position in this list.
def bucket_start_times(anchor: datetime, window_s: int, bucket_s: int) -> list[datetime]:
    n = window_s // bucket_s
    elapsed = int((anchor - GRID_ORIGIN).total_seconds())
    last = GRID_ORIGIN + timedelta(seconds=elapsed - (elapsed % bucket_s))
    return [last - timedelta(seconds=(n - 1 - i) * bucket_s) for i in range(n)]


def _round1(v: float | None) -> float | None:
    return round(v, 1) if v is not None else None


def _as_float(v: Decimal | None) -> float | None:
    return float(v) if v is not None else None


# meiva:note
# Run a query assembled from integers and the registered payload-key constants; every row comes
# back as `row_class`, whose fields are named as the query's columns.
def _rows[RowT](
    conn: psycopg.Connection, row_class: type[RowT], query: str, params: tuple[object, ...]
) -> list[RowT]:
    return query_all(conn, row_class, query, params)  # pyright: ignore[reportArgumentType]


# meiva:note
# One bucket's SSE / event-log drop counts.
@dataclass(frozen=True)
class _SseRow:
    bkt: int
    queue_full: int
    publish_error: int
    event_log_drop: int


# meiva:note
# One bucket's LLM profile, or the whole-window row (`is_total` set by `grouping(bkt)`). The sums are
# numeric (`Decimal`), the percentiles double precision, and the latencies NULL when no row of the
# group carried one.
@dataclass(frozen=True)
class _LlmRow:
    bkt: int | None
    calls: int
    tokens_in: Decimal
    tokens_out: Decimal
    tokens_reasoning: Decimal
    lat_sum: Decimal
    lat_max: Decimal | None
    p50: float | None
    p95: float | None
    errors: int
    is_total: int


# meiva:note
# One bucket's service start count.
@dataclass(frozen=True)
class _RestartRow:
    bkt: int
    starts: int


# meiva:note
# One service's start count and latest start inside the window.
@dataclass(frozen=True)
class _ServiceRow:
    service: str
    starts: int
    last_start: datetime


# meiva:note
# The SQL expression of an event's bucket index, counted from the first bucket.
def _bucket(start_s: int, bucket_s: int) -> str:
    return f"floor((extract(epoch FROM ts) - {int(start_s)}) / {int(bucket_s)})::int"


# meiva:note
# The bucket grid of one request and the query parameters that bound it.
class _Window:
    def __init__(self, bucket_starts: list[datetime], bucket_s: int) -> None:
        self.bucket_s = bucket_s
        self.n = len(bucket_starts)
        self.start = bucket_starts[0]
        self.end = bucket_starts[-1] + timedelta(seconds=bucket_s)
        self.bucket_sql = _bucket(int(self.start.timestamp()), bucket_s)

    def zeros(self) -> list[int]:
        return [0] * self.n

    def fill[T](self, rows: dict[int, T], default: T) -> list[T]:
        return [rows.get(i, default) for i in range(self.n)]


# meiva:note
# keys come from the registered payload constants.
def _sse_series(conn: psycopg.Connection, window: _Window) -> SseReport:
    kind = f"COALESCE({SSE_DROP_KEYS['kind']}, '')"
    rows = _rows(
        conn,
        _SseRow,
        f"""
        SELECT {window.bucket_sql} AS bkt,
               count(*) FILTER (WHERE event_name = 'sse_drop' AND {kind} = 'queue_full')
                 AS queue_full,
               count(*) FILTER (WHERE event_name = 'sse_drop'
                                AND {kind} NOT IN ('', 'queue_full')) AS publish_error,
               count(*) FILTER (WHERE event_name = 'event_log_drop') AS event_log_drop
        FROM telemetry_events
        WHERE event_name IN ('sse_drop', 'event_log_drop') AND ts >= %s AND ts < %s
        GROUP BY bkt
        """,  # noqa: S608
        (window.start, window.end),
    )
    by_bucket = {r.bkt: r for r in rows}
    empty = _SseRow(0, 0, 0, 0)
    cells = window.fill(by_bucket, empty)
    return SseReport(
        series=[
            SseDropBucket(
                bucket=i,
                queue_full=c.queue_full,
                publish_error=c.publish_error,
                event_log_drop=c.event_log_drop,
            )
            for i, c in enumerate(cells)
        ],
        totals=SseTotals(
            queue_full=sum(c.queue_full for c in cells),
            publish_error=sum(c.publish_error for c in cells),
            event_log_drop=sum(c.event_log_drop for c in cells),
        ),
    )


# meiva:note
# keys come from the registered payload constants.
def _llm_series(conn: psycopg.Connection, window: _Window) -> LlmReport:
    latency = numeric(LLM_USAGE_KEYS["latency_ms"])
    usage = "event_name = 'llm_usage'"
    rows = _rows(
        conn,
        _LlmRow,
        f"""
        SELECT bkt,
               count(*) FILTER (WHERE {usage}) AS calls,
               COALESCE(sum(tin), 0) AS tokens_in, COALESCE(sum(tout), 0) AS tokens_out,
               COALESCE(sum(treason), 0) AS tokens_reasoning,
               COALESCE(sum(lat), 0) AS lat_sum, max(lat) AS lat_max,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY lat) AS p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY lat) AS p95,
               count(*) FILTER (WHERE event_name = ANY(%s)) AS errors,
               grouping(bkt) AS is_total
        FROM (
          SELECT {window.bucket_sql} AS bkt, event_name,
                 CASE WHEN {usage} THEN {numeric(LLM_USAGE_KEYS["in_total"])} END AS tin,
                 CASE WHEN {usage} THEN {numeric(LLM_USAGE_KEYS["out_total"])} END AS tout,
                 CASE WHEN {usage} THEN {numeric(LLM_USAGE_KEYS["reasoning"])} END AS treason,
                 CASE WHEN {usage} THEN {latency} END AS lat
          FROM telemetry_events
          WHERE (event_name = 'llm_usage' OR event_name = ANY(%s)) AND ts >= %s AND ts < %s
        ) s
        GROUP BY GROUPING SETS ((bkt), ())
        """,  # noqa: S608
        (_LLM_ERROR_EVENTS, _LLM_ERROR_EVENTS, window.start, window.end),
    )
    per_bucket: dict[int, _LlmRow] = {}
    total: _LlmRow | None = None
    for row in rows:
        if row.is_total:
            total = row
        elif row.bkt is not None:
            per_bucket[row.bkt] = row
    empty = _LlmRow(None, 0, Decimal(0), Decimal(0), Decimal(0), Decimal(0), None, None, None, 0, 0)
    cells = window.fill(per_bucket, empty)
    series = [_llm_bucket(i, cell) for i, cell in enumerate(cells)]
    return LlmReport(series=series, totals=_llm_totals(cells, series, total))


# meiva:note
# One bucket's LLM profile: counts as stored, token and latency sums rounded to whole numbers,
# `tps` = tokens per second of summed latency (None without latency data).
def _llm_bucket(index: int, cell: _LlmRow) -> LlmBucket:
    tokens_in = round(cell.tokens_in)
    tokens_out = round(cell.tokens_out)
    tokens_reasoning = round(cell.tokens_reasoning)
    lat_sum = round(cell.lat_sum)
    tps = None
    if lat_sum:
        tps = _round1((tokens_in + tokens_out + tokens_reasoning) / (lat_sum / 1000.0))
    return LlmBucket(
        bucket=index,
        calls=cell.calls,
        latency_p50_ms=_round1(cell.p50),
        latency_p95_ms=_round1(cell.p95),
        latency_max_ms=_round1(_as_float(cell.lat_max)),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        tps=tps,
        errors=cell.errors,
    )


# meiva:note
# The whole-window profile: sums of the buckets, latency percentiles and max from the
# grouping-set total row.
def _llm_totals(cells: list[_LlmRow], series: list[LlmBucket], total: _LlmRow | None) -> LlmTotals:
    t_tokens = sum(b.tokens_in + b.tokens_out for b in series) + sum(
        round(c.tokens_reasoning) for c in cells
    )
    t_lat = sum(round(c.lat_sum) for c in cells)
    return LlmTotals(
        calls=sum(c.calls for c in cells),
        latency_p50_ms=_round1(total.p50) if total else None,
        latency_p95_ms=_round1(total.p95) if total else None,
        latency_max_ms=_round1(_as_float(total.lat_max)) if total else None,
        tokens_in=sum(b.tokens_in for b in series),
        tokens_out=sum(b.tokens_out for b in series),
        tps=_round1(t_tokens / (t_lat / 1000.0)) if t_lat else None,
        errors=sum(c.errors for c in cells),
    )


# meiva:note
# the bucket expression is built from integers.
# keys come from the registered payload constants.
def _restart_series(conn: psycopg.Connection, window: _Window) -> RestartReport:
    rows = _rows(
        conn,
        _RestartRow,
        f"""
        SELECT {window.bucket_sql} AS bkt, count(*) AS starts
        FROM telemetry_events
        WHERE event_name = 'service_started' AND ts >= %s AND ts < %s
        GROUP BY bkt
        """,  # noqa: S608
        (window.start, window.end),
    )
    cells = window.fill({r.bkt: r.starts for r in rows}, 0)

    name = f"btrim(COALESCE({SERVICE_STARTED_KEYS['name']}, ''))"
    services = [
        ServiceRestartRow(
            name=r.service, starts=r.starts, last_start=r.last_start.astimezone(UTC).isoformat()
        )
        for r in _rows(
            conn,
            _ServiceRow,
            f"""
            SELECT {name} AS service, count(*) AS starts, max(ts) AS last_start
            FROM telemetry_events
            WHERE event_name = 'service_started' AND ts >= %s AND ts < %s AND {name} <> ''
            GROUP BY service ORDER BY count(*) DESC, service
            """,  # noqa: S608
            (window.start, window.end),
        )
    ]
    return RestartReport(
        series=[RestartBucket(bucket=i, service_starts=s) for i, s in enumerate(cells)],
        services=services,
        totals=RestartTotals(service_starts=sum(cells)),
    )


# meiva:note
# Run every registered ops series for `window` and return the full report: meta + the three groups.
def fetch_ops_series(
    conn: psycopg.Connection, window: str, *, anchor: datetime
) -> OpsMonitorReport:
    window_s, bucket_s = WINDOWS[window]
    bucket_starts = bucket_start_times(anchor, window_s, bucket_s)
    grid = _Window(bucket_starts, bucket_s)
    return OpsMonitorReport(
        meta=OpsMonitorMeta(
            window=window,
            bucket_seconds=bucket_s,
            generated_at=anchor.isoformat(),
            bucket_starts=[b.isoformat() for b in bucket_starts],
        ),
        sse=_sse_series(conn, grid),
        llm=_llm_series(conn, grid),
        restarts=_restart_series(conn, grid),
    )
