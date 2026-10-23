# meiva:note
# The stats dashboard's window totals, read from `telemetry_events`.
#
# One scan of the window's `llm_usage` and `turn_end` rows gives the token, cost and turn
# sums. The cluster filter accepts the home cluster's label and rows with no label, as every
# dashboard read does. The warning/error classes are `gateway.cluster.monitor.alert_classes`.

from __future__ import annotations

from datetime import datetime
from typing import Any, NamedTuple

import psycopg

from base.api_contracts.agent_contract import HARNESS_BILLING, HarnessBilling
from base.events.contract import LLM_USAGE_KEYS, TURN_END_KEYS
from base.telemetry.metrics.event_sql import numeric

SUBSCRIPTION_SOURCES = tuple(
    sorted(h.value for h, b in HARNESS_BILLING.items() if b is HarnessBilling.SUBSCRIPTION)
)
_SUBSCRIPTION_SOURCES_SQL = ", ".join(repr(s) for s in SUBSCRIPTION_SOURCES)


# meiva:note
# Bound the rest of the current transaction's statements to `ms` milliseconds.
def set_statement_timeout(target: psycopg.Connection[Any] | psycopg.Cursor[Any], ms: int) -> None:
    target.execute("SELECT set_config('statement_timeout', %s, true)", (str(ms),))


# meiva:note
# Token, cost and turn sums over one window.
class WindowTotals(NamedTuple):
    in_total: int
    out_total: int
    cache_read: int
    cache_input: int
    cost_usd: float
    turn_seconds: float
    turn_count: int


# meiva:note
# Tokens, cost and turn duration over `(start, end]`.
#
# Cost is the usage-time snapshot each row carries, never today's registry price. A value
# that is missing or not a number is left out of its sum; the turn count is the number of
# successful `turn_end` rows, with or without a duration.
# meiva:note
# keys come from the registered payload constants.
# an aggregate always returns one row.
def window_totals(
    conn: psycopg.Connection[Any], *, cluster: str, start: datetime, end: datetime
) -> WindowTotals:
    usage = "event_name = 'llm_usage' AND category = 'telemetry'"
    api_usage = (
        f"{usage} AND COALESCE({LLM_USAGE_KEYS['source']}, '') NOT IN ({_SUBSCRIPTION_SOURCES_SQL})"
    )
    turn = f"event_name = 'turn_end' AND {TURN_END_KEYS['ok']} = 'true'"
    query = f"""
        SELECT
          COALESCE(sum({numeric(LLM_USAGE_KEYS["in_total"])}) FILTER (WHERE {usage}), 0),
          COALESCE(sum({numeric(LLM_USAGE_KEYS["out_total"])}) FILTER (WHERE {usage}), 0),
          COALESCE(sum({numeric(LLM_USAGE_KEYS["cache_read"])}) FILTER (WHERE {usage}), 0),
          COALESCE(sum({numeric(LLM_USAGE_KEYS["cost_usd"])}) FILTER (WHERE {api_usage}), 0),
          COALESCE(sum({numeric(TURN_END_KEYS["duration_seconds"])}) FILTER (WHERE {turn}), 0),
          count(*) FILTER (WHERE {turn}),
          COALESCE(sum({numeric(LLM_USAGE_KEYS["in_total"])}) FILTER (
              WHERE {usage} AND {numeric(LLM_USAGE_KEYS["cache_read"])} IS NOT NULL), 0)
        FROM telemetry_events
        WHERE event_name IN ('llm_usage', 'turn_end')
          AND (cluster = %s OR cluster = '')
          AND ts > %s AND ts <= %s
    """  # noqa: S608
    row = conn.execute(query, (cluster, start, end)).fetchone()  # pyright: ignore[reportArgumentType]
    assert row is not None  # noqa: S101
    return WindowTotals(
        in_total=round(row[0]),
        out_total=round(row[1]),
        cache_read=round(row[2]),
        cost_usd=float(row[3]),
        turn_seconds=float(row[4]),
        turn_count=int(row[5]),
        cache_input=round(row[6]),
    )
