# meiva:link gateway/inspect/docs/inspect.md

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from base.clock import Clock
from gateway.inspect.schemas import AgentCost
from gateway.schemas.stats import StatsWindowHours, window_delta

Row = dict[str, object]


def _row(conn: Connection, query: str, params: tuple[object, ...]) -> Row:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)  # pyright: ignore[reportArgumentType] -- fixed internal SQL
        return cur.fetchall()[0]


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise TypeError(f"expected a number, got {value!r}")
    return int(value)


def _ledger_costs(conn: Connection, agent_id: int, start: datetime, end: datetime) -> Row:
    return _row(
        conn,
        """
        SELECT count(*) FILTER (WHERE NOT complete) AS incomplete_reports,
          COALESCE(sum(calls), 0) AS usage_calls,
          COALESCE(sum(calls) FILTER (WHERE cost_usd IS NULL), 0) AS unpriced_calls,
          COALESCE(sum(calls) FILTER (
              WHERE cost_usd IS NULL AND billing = 'subscription'), 0) AS unpriced_calls_subscription,
          COALESCE(sum(tokens_in), 0) AS tokens_in,
          COALESCE(sum(tokens_out), 0) AS tokens_out,
          COALESCE(sum(tokens_in) FILTER (WHERE cache_read IS NOT NULL), 0) AS tokens_cache_input,
          COALESCE(sum(cache_read), 0) AS tokens_cached,
          COALESCE(sum(tokens_reasoning), 0) AS tokens_reasoning,
          COALESCE(sum(cost_usd), 0) AS cost_usd,
          COALESCE(sum(cost_usd) FILTER (WHERE billing = 'subscription'), 0) AS cost_usd_subscription
        FROM llm_usage_ledger
        WHERE agent_id = %s AND occurred_at >= %s AND occurred_at < %s
        """,
        (agent_id, start, end),
    )


def read_cost(conn: Connection, agent_id: int, start: datetime, end: datetime) -> AgentCost:
    """One agent's ledger usage over `[start, end)`."""
    row = _ledger_costs(conn, agent_id, start, end)
    cost_usd = Decimal(str(row["cost_usd"]))
    subscription = Decimal(str(row["cost_usd_subscription"]))
    tokens_in = _as_int(row["tokens_in"])
    tokens_cached = _as_int(row["tokens_cached"])
    cache_input = _as_int(row["tokens_cache_input"])
    unpriced = _as_int(row["unpriced_calls"])
    unpriced_subscription = _as_int(row["unpriced_calls_subscription"])
    return AgentCost(
        cost_usd=float(round(cost_usd, 4)),
        cost_usd_api=float(round(cost_usd - subscription, 4)),
        cost_usd_subscription=float(round(subscription, 4)),
        unpriced_calls=unpriced,
        unpriced_calls_api=unpriced - unpriced_subscription,
        unpriced_calls_subscription=unpriced_subscription,
        llm_calls=_as_int(row["usage_calls"]),
        incomplete_usage_reports=_as_int(row["incomplete_reports"]),
        tokens_in=tokens_in,
        tokens_out=_as_int(row["tokens_out"]),
        tokens_cached=tokens_cached,
        tokens_reasoning=_as_int(row["tokens_reasoning"]),
        cache_hit_pct=min(100, round(tokens_cached / cache_input * 100, 2))
        if cache_input
        else (None if tokens_in else 0),
    )


def inspect_cost(
    pool: ConnectionPool,
    agent_id: int,
    hours: StatsWindowHours | None,
    *,
    spawned_at: datetime,
    clock: Clock,
) -> AgentCost:
    """One agent's usage over the window, read from the usage ledger in one snapshot.

    The window is `[spawned_at, now)` for the cumulative view and `[now - hours, now)` otherwise.
    """
    end = clock.now()
    start = spawned_at if hours is None else end - window_delta(hours)
    with pool.connection(timeout=1.0) as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '2s'")
        conn.execute("SET LOCAL lock_timeout = '200ms'")
        return read_cost(conn, agent_id, start, end)
