"""SQL-only Inspector statistics over persisted observations and historical days.

New observations own dates at/after the collection's UTC cutover day. Earlier
full days prefer the existing family ledger; raw observations fill missing days
and partial-day edges. The two sources never add the same family's day twice.
Exact quantiles retain their observations; their DB work scales with the selected
turns. Source loss remains explicit and is never repaired by a synchronous log scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from base.clock import Clock
from base.host.boundary.json_values import is_object_dict
from gateway.inspect._metrics_health import note_inspect_metrics_coverage
from gateway.inspect.schemas import (
    AgentActivity,
    AgentCost,
    AgentStats,
    AgentTps,
    InspectMetricsMetadata,
    MetricEvidence,
)
from gateway.schemas.stats import StatsWindowHours, window_delta

_SUM_FIELDS = (
    "usage_calls",
    "unpriced_calls",
    "tokens_in",
    "tokens_out",
    "tokens_cached",
    "tokens_cache_input",
    "tokens_reasoning",
    "cost_usd",
    "cost_usd_subscription",
    "unpriced_calls_subscription",
    "turn_total",
    "turn_ok",
    "turn_duration_sum",
    "exec_ok",
    "exec_failed",
    "active_seconds",
    "exec_seconds",
)


@dataclass(frozen=True)
class MetricsSnapshot:
    """One persisted statistics result; unavailable sections have no invented value."""

    cost: AgentCost | None
    stats: AgentStats | None
    tps: AgentTps | None
    activity: AgentActivity | None
    metadata: InspectMetricsMetadata


def _midnight(value: datetime) -> datetime:
    return datetime.combine(value.astimezone(UTC).date(), time.min, tzinfo=UTC)


def _full_day(day: date, start: datetime, end: datetime) -> bool:
    midnight = datetime.combine(day, time.min, tzinfo=UTC)
    return start <= midnight and midnight + timedelta(days=1) <= end


def _weighted_quantile(q: float, values: list[tuple[float, int]]) -> float:
    """Linear-interpolated sample percentile without expanding repeated values."""
    ordered = sorted(values)
    total = sum(count for _, count in ordered)
    if not total:
        return 0.0
    rank = q * (total - 1)
    lower = int(rank)
    upper = min(lower + 1, total - 1)
    count_so_far = 0
    low_value = high_value = 0.0
    for value, count in ordered:
        if count_so_far <= lower < count_so_far + count:
            low_value = value
        if count_so_far <= upper < count_so_far + count:
            high_value = value
            break
        count_so_far += count
    return low_value + (high_value - low_value) * (rank - lower)


# meiva:note
# One result row by column name. The values are what psycopg adapted (numeric -> `Decimal`, bigint
# -> `int`, date -> `date`, jsonb -> `dict`), still `object` here; the `_as_*` readers below
# narrow the one column a caller needs.
Row = dict[str, object]


def _rows(conn: Connection, query: str, params: tuple[object, ...]) -> list[Row]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)  # pyright: ignore[reportArgumentType] -- fixed internal SQL
        return list(cur.fetchall())


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise TypeError(f"expected a number, got {value!r}")
    return int(value)


def _as_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise TypeError(f"expected a number, got {value!r}")
    return float(value)


def _as_day(value: object) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"expected a date, got {value!r}")
    return value


def _as_moment(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"expected a timestamp, got {value!r}")
    return value


def _observed_days(
    conn: Connection, agent_id: int, start: datetime, end: datetime
) -> dict[date, Row]:
    """Read full-day summaries and reduce only the at-most-two boundary days."""
    full_from = _midnight(start)
    if full_from < start:
        full_from += timedelta(days=1)
    full_to = _midnight(end)
    days = _rows(
        conn,
        "SELECT * FROM agent_metric_days WHERE agent_id=%s AND day>=%s AND day<%s",
        (agent_id, full_from.date(), full_to.date()),
    )
    edges = [(start, min(full_from, end)), (max(full_to, start), end)]
    merged_edges: list[tuple[datetime, datetime]] = []
    for lo, hi in sorted(set(edges)):
        if lo >= hi or (merged_edges and lo < merged_edges[-1][1]):
            continue
        merged_edges.append((lo, hi))
    # Fixed column vocabulary, never request-derived SQL identifiers.
    expressions = {
        "turn_duration_sum": "turn_duration_seconds",
        "tokens_cache_input": "coalesce(tokens_cache_input, tokens_in)",
    }
    sums = ", ".join(
        f"COALESCE(sum({expressions.get(name, name)}),0) AS {name}" for name in _SUM_FIELDS
    )
    for lo, hi in merged_edges:
        days.extend(
            _rows(
                conn,
                "SELECT (occurred_at AT TIME ZONE 'UTC')::date AS day, "  # noqa: S608 -- fixed metric vocabulary
                + sums
                + ", min(turn_duration_seconds) AS turn_duration_min, "
                "max(turn_duration_seconds) AS turn_duration_max, "
                "max(observed_at) AS last_observed_at FROM agent_metric_observations "
                "WHERE agent_id=%s AND occurred_at>=%s AND occurred_at<%s GROUP BY 1",
                (agent_id, lo, hi),
            )
        )
    return {_as_day(row["day"]): row for row in days}


def _legacy_days(
    conn: Connection,
    agent_id: int,
    start: datetime,
    end: datetime,
    cutover: date,
) -> tuple[dict[date, Row], dict[date, Row]]:
    """Read historical evidence without allowing the old writer to own new dates."""
    params = (agent_id, start.date(), min(end.date(), cutover))
    costs = _rows(
        conn,
        "SELECT day, sum(llm_calls) AS usage_calls, sum(unpriced_calls) AS unpriced_calls, "
        "sum(unpriced_calls_subscription) AS unpriced_calls_subscription, "
        "sum(tokens_in) AS tokens_in, sum(tokens_out) AS tokens_out, "
        "sum(tokens_in) AS tokens_cache_input, "
        "sum(tokens_cached) AS tokens_cached, sum(tokens_reasoning) AS tokens_reasoning, "
        "sum(cost_usd) AS cost_usd, sum(cost_usd_subscription) AS cost_usd_subscription "
        "FROM agent_model_tokens_daily "
        "WHERE agent_id=%s AND day>=%s AND day<%s GROUP BY day",
        params,
    )
    turns = _rows(
        conn,
        "SELECT day, turn_total, turn_ok, turn_dur_sum AS turn_duration_sum, "
        "turn_dur_min AS turn_duration_min, turn_dur_max AS turn_duration_max, "
        "turn_dur_hist, exec_ok, exec_failed FROM agent_metrics_daily "
        "WHERE agent_id=%s AND day>=%s AND day<%s",
        params,
    )
    return (
        {_as_day(row["day"]): row for row in costs if _full_day(_as_day(row["day"]), start, end)},
        {_as_day(row["day"]): row for row in turns if _full_day(_as_day(row["day"]), start, end)},
    )


def _evidence(
    *,
    historical: bool,
    present: bool,
    sources: list[str],
    precision: str | None = None,
) -> MetricEvidence:
    availability = "partial" if historical else "observed"
    if historical and not present:
        availability = "unavailable"
    return MetricEvidence(
        availability=availability,  # pyright: ignore[reportArgumentType]
        sources=sources,
        reason="historical_coverage_unknown" if historical else None,
        duration_precision=precision,  # pyright: ignore[reportArgumentType]
    )


def _alive_seconds(
    conn: Connection,
    agent_id: int,
    start: datetime,
    end: datetime,
) -> tuple[float, bool]:
    rows = _rows(
        conn,
        "SELECT COALESCE(sum(EXTRACT(epoch FROM "
        "least(COALESCE(ended_at,%s),%s)-greatest(started_at,%s))),0) AS seconds, "
        "count(*) AS intervals FROM agent_lifecycle_intervals "
        "WHERE agent_id=%s AND started_at<%s AND COALESCE(ended_at,%s)>%s",
        (end, end, start, agent_id, end, end, start),
    )
    return _as_float(rows[0]["seconds"]), bool(rows[0]["intervals"])


def inspect_snapshot(
    pool: ConnectionPool,
    agent_id: int,
    hours: StatsWindowHours | None,
    *,
    spawned_at: datetime,
    clock: Clock,
) -> MetricsSnapshot:
    """Read one consistent DB snapshot; no HTTP/Loki request belongs on this path.

    Historical evidence remains partial even after a successful backfill, because the old
    best-effort event producer cannot certify that every observation survived.
    """
    sampled_at = clock.now()
    start = spawned_at if hours is None else sampled_at - window_delta(hours)
    with pool.connection(timeout=1.0) as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '2s'")
        conn.execute("SET LOCAL lock_timeout = '200ms'")
        collection = _as_moment(
            _rows(conn, "SELECT started_at FROM agent_metric_collection", ())[0]["started_at"]
        )
        snapshot = read_snapshot(conn, agent_id, start, sampled_at, spawned_at, collection)
    # Outside the read transaction: the coverage note is a diagnostic side
    # channel (background log + event; task #3869) and never joins
    # the snapshot itself.
    note_inspect_metrics_coverage(agent_id, snapshot.metadata, spawned_at=spawned_at)
    return snapshot


# meiva:note
# The window's additive totals: counts as integers, durations as seconds, cost as an exact decimal.
@dataclass
class _Totals:
    usage_calls: int = 0
    unpriced_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    tokens_cache_input: int = 0
    tokens_reasoning: int = 0
    cost_usd: Decimal = field(default_factory=lambda: Decimal(0))
    cost_usd_subscription: Decimal = field(default_factory=lambda: Decimal(0))
    unpriced_calls_subscription: int = 0
    turn_total: int = 0
    turn_ok: int = 0
    turn_duration_sum: float = 0.0
    exec_ok: int = 0
    exec_failed: int = 0
    active_seconds: float = 0.0
    exec_seconds: float = 0.0


def _sum_days(
    observed: dict[date, Row], costs: dict[date, Row], turns: dict[date, Row]
) -> tuple[_Totals, list[float]]:
    """Choose one cost and turn source per day before additive reduction."""
    totals = _Totals()
    extrema: list[float] = []
    for day in observed.keys() | costs.keys() | turns.keys():
        raw = observed.get(day, {})
        cost_row = costs.get(day, raw)
        turn_row = turns.get(day, raw)
        totals.usage_calls += _as_int(cost_row.get("usage_calls", 0))
        totals.unpriced_calls += _as_int(cost_row.get("unpriced_calls", 0))
        totals.tokens_in += _as_int(cost_row.get("tokens_in", 0))
        totals.tokens_out += _as_int(cost_row.get("tokens_out", 0))
        totals.tokens_cached += _as_int(cost_row.get("tokens_cached", 0))
        cache_input = cost_row.get("tokens_cache_input", 0)
        totals.tokens_cache_input += _as_int(
            cost_row["tokens_in"] if cache_input is None else cache_input
        )
        totals.tokens_reasoning += _as_int(cost_row.get("tokens_reasoning", 0))
        totals.cost_usd += Decimal(str(cost_row.get("cost_usd", 0)))
        totals.cost_usd_subscription += Decimal(str(cost_row.get("cost_usd_subscription", 0)))
        totals.unpriced_calls_subscription += _as_int(
            cost_row.get("unpriced_calls_subscription", 0)
        )
        totals.turn_total += _as_int(turn_row.get("turn_total", 0))
        totals.turn_ok += _as_int(turn_row.get("turn_ok", 0))
        totals.turn_duration_sum += _as_float(turn_row.get("turn_duration_sum", 0))
        totals.exec_ok += _as_int(turn_row.get("exec_ok", 0))
        totals.exec_failed += _as_int(turn_row.get("exec_failed", 0))
        totals.active_seconds += _as_float(raw.get("active_seconds", 0))
        totals.exec_seconds += _as_float(raw.get("exec_seconds", 0))
        for key in ("turn_duration_min", "turn_duration_max"):
            value = turn_row.get(key)
            if value is not None:
                extrema.append(_as_float(value))
    return totals, extrema


# meiva:note
# The (seconds, count) samples of the days whose exact durations are not trusted, read from their
# daily histograms, and whether any histogram was non-empty.
def _histogram_samples(bucket_days: dict[date, Row]) -> tuple[list[tuple[float, int]], bool]:
    samples: list[tuple[float, int]] = []
    bucketed = False
    for row in bucket_days.values():
        histogram = row["turn_dur_hist"]
        if not is_object_dict(histogram):
            raise TypeError("turn_dur_hist must be a JSON object")
        bucketed |= bool(histogram)
        samples.extend((float(value), _as_int(count)) for value, count in histogram.items())
    return samples, bucketed


def _duration_distribution(
    conn: Connection,
    agent_id: int,
    start: datetime,
    end: datetime,
    observed: dict[date, Row],
    turns: dict[date, Row],
) -> tuple[list[tuple[float, int]], str]:
    # Group exact samples once. An old daily histogram remains authoritative
    # until all of its turn duration observations survive replay; equal event
    # counts alone do not prove that optional durations were recorded.
    exact_rows = _rows(
        conn,
        "SELECT (occurred_at AT TIME ZONE 'UTC')::date AS day, "
        "turn_duration_seconds AS duration, count(*) AS n "
        "FROM agent_metric_observations WHERE agent_id=%s AND occurred_at>=%s "
        "AND occurred_at<%s AND turn_duration_seconds IS NOT NULL "
        "GROUP BY 1,2",
        (agent_id, start, end),
    )
    duration_counts: dict[date, int] = {}
    for row in exact_rows:
        day = _as_day(row["day"])
        duration_counts[day] = duration_counts.get(day, 0) + _as_int(row["n"])
    bucket_days = {
        day: row
        for day, row in turns.items()
        if day not in observed
        or observed[day]["turn_total"] != row["turn_total"]
        or duration_counts.get(day, 0) != row["turn_total"]
    }
    exact = [row for row in exact_rows if _as_day(row["day"]) not in bucket_days]
    distribution = [(_as_float(row["duration"]), _as_int(row["n"])) for row in exact]
    buckets, bucketed = _histogram_samples(bucket_days)
    distribution.extend(buckets)
    precision = "mixed" if bucketed and exact else "one_second_buckets" if bucketed else "exact"
    return distribution, precision


def _cost_and_turns(
    totals: _Totals,
    extrema: list[float],
    distribution: list[tuple[float, int]],
    cost_evidence: MetricEvidence,
    turn_evidence: MetricEvidence,
) -> tuple[AgentCost | None, AgentStats | None]:
    """Project numeric evidence; absent families and durations remain null."""
    cost = (
        None
        if cost_evidence.availability == "unavailable"
        else AgentCost(
            cost_usd=float(round(totals.cost_usd, 4)),
            cost_usd_api=float(round(totals.cost_usd - totals.cost_usd_subscription, 4)),
            cost_usd_subscription=float(round(totals.cost_usd_subscription, 4)),
            unpriced_calls=totals.unpriced_calls,
            unpriced_calls_api=totals.unpriced_calls - totals.unpriced_calls_subscription,
            unpriced_calls_subscription=totals.unpriced_calls_subscription,
            llm_calls=totals.usage_calls,
            tokens_in=totals.tokens_in,
            tokens_out=totals.tokens_out,
            tokens_cached=totals.tokens_cached,
            tokens_reasoning=totals.tokens_reasoning,
            cache_hit_pct=min(100, round(totals.tokens_cached / totals.tokens_cache_input * 100, 2))
            if totals.tokens_cache_input
            else (None if totals.tokens_in else 0),
        )
    )
    stats = (
        None
        if turn_evidence.availability == "unavailable"
        else AgentStats(
            turn_total=totals.turn_total,
            turn_ok=totals.turn_ok,
            turn_p50_seconds=round(_weighted_quantile(0.5, distribution), 2)
            if distribution or not totals.turn_total
            else None,
            turn_p90_seconds=round(_weighted_quantile(0.9, distribution), 2)
            if distribution or not totals.turn_total
            else None,
            turn_min_seconds=round(min(extrema, default=0), 2)
            if extrema or not totals.turn_total
            else None,
            turn_max_seconds=round(max(extrema, default=0), 2)
            if extrema or not totals.turn_total
            else None,
            exec_ok=totals.exec_ok,
            exec_failed=totals.exec_failed,
        )
    )
    return cost, stats


def _throughput_and_activity(
    totals: _Totals,
    cost: AgentCost | None,
    stats: AgentStats | None,
    *,
    durations_known: bool,
    historical: bool,
    alive: float,
    whole_alive: float,
    metadata: InspectMetricsMetadata,
) -> tuple[AgentTps | None, AgentActivity | None]:
    # Whole-lifetime coverage is independent from a recent selected window.
    # Preserve LM-stage throughput even when the lifetime denominator is unknown.
    tps = None
    activity = None
    if cost is not None and stats is not None:
        tps = AgentTps(
            lm_stage_tps=(
                round(totals.tokens_out / totals.turn_duration_sum, 2)
                if totals.turn_duration_sum
                else 0
            )
            if durations_known
            else None,
            agent_lifecycle_tps=(round(totals.tokens_out / whole_alive, 2) if whole_alive else 0)
            if metadata.lifecycle.availability == "observed"
            else None,
        )
    if not historical and metadata.activity.availability != "unavailable":
        activity = AgentActivity(
            active_seconds=round(totals.active_seconds, 2),
            alive_seconds=round(alive, 2),
            active_rate=min(1, round(totals.active_seconds / alive, 4)) if alive else 0,
            llm_seconds=round(totals.turn_duration_sum, 2) if durations_known else None,
            exec_seconds=round(totals.exec_seconds, 2),
        )
    return tps, activity


def _turn_evidence(
    *,
    historical: bool,
    turns: dict[date, Row],
    observed: dict[date, Row],
    distribution: list[tuple[float, int]],
    precision: str,
    durations_known: bool,
) -> MetricEvidence:
    """The turn counters' evidence block: availability, precision and any retained archive source."""
    turn_evidence = _evidence(
        historical=historical,
        present=bool(turns)
        or any(
            row["turn_total"] or row["exec_ok"] or row["exec_failed"] for row in observed.values()
        ),
        sources=["observations", *(["historical_daily_turns"] if turns else [])],
        precision=precision if distribution else None,
    )
    if not durations_known:
        turn_evidence = turn_evidence.model_copy(
            update={
                "availability": "partial",
                "reason": "missing_turn_durations",
            }
        )
    return turn_evidence


def read_snapshot(
    conn: Connection,
    agent_id: int,
    start: datetime,
    end: datetime,
    spawned_at: datetime,
    collection: datetime,
) -> MetricsSnapshot:
    observed = _observed_days(conn, agent_id, start, end)
    # Birth cannot have observations before itself; its first partial UTC day
    # is therefore a complete lifetime ledger day without estimating an edge.
    legacy_start = _midnight(start) if start == spawned_at else start
    costs, turns = _legacy_days(
        conn, agent_id, legacy_start, end, collection.astimezone(UTC).date()
    )
    totals, extrema = _sum_days(observed, costs, turns)
    distribution, precision = _duration_distribution(conn, agent_id, start, end, observed, turns)
    durations_known = sum(count for _, count in distribution) == totals.turn_total
    historical = max(start, spawned_at) < collection
    cost_evidence = _evidence(
        historical=historical,
        present=bool(costs) or any(row["usage_calls"] for row in observed.values()),
        sources=["observations", *(["historical_daily_costs"] if costs else [])],
    )
    turn_evidence = _turn_evidence(
        historical=historical,
        turns=turns,
        observed=observed,
        distribution=distribution,
        precision=precision,
        durations_known=durations_known,
    )
    activity_evidence = _evidence(
        historical=historical, present=bool(observed), sources=["observations"]
    )
    alive, intervals = _alive_seconds(conn, agent_id, start, end)
    whole_alive, _ = _alive_seconds(conn, agent_id, spawned_at, end)
    lifecycle_evidence = _evidence(
        historical=spawned_at < collection, present=intervals, sources=["state_transitions"]
    )
    metadata = InspectMetricsMetadata(
        window_start=start,
        window_end=end,
        sampled_at=end,
        collection_started_at=collection,
        last_observed_at=max(
            (_as_moment(r["last_observed_at"]) for r in observed.values()), default=None
        ),
        cost=cost_evidence,
        turns=turn_evidence,
        activity=activity_evidence,
        lifecycle=lifecycle_evidence,
    )
    cost, stats = _cost_and_turns(totals, extrema, distribution, cost_evidence, turn_evidence)
    tps, activity = _throughput_and_activity(
        totals,
        cost,
        stats,
        durations_known=durations_known,
        historical=historical,
        alive=alive,
        whole_alive=whole_alive,
        metadata=metadata,
    )
    return MetricsSnapshot(cost, stats, tps, activity, metadata)
