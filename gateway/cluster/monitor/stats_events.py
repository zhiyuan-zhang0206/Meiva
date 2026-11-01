# meiva:note
# The stats dashboard's window totals, read from `telemetry_events`.
#
# One scan of the window's `llm_usage` rows gives the token sums, total metered API cost, and
# per-model metered API cost slices. Subscription harness rows (`SUBSCRIPTION_SOURCES`) still
# contribute to token totals while being excluded from metered API cost sums. The cluster filter
# accepts the home cluster's label and rows with no label, as every dashboard read does. The
# warning/error classes are `gateway.cluster.monitor.alert_classes`.
# meiva:link gateway/cluster/docs/ops-surfaces.meiva.okf.md

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, NamedTuple

import psycopg

from base.api_contracts.agent_contract import HARNESS_BILLING, HarnessBilling
from base.events.contract import LLM_USAGE_KEYS
from base.telemetry.metrics.event_sql import numeric

SUBSCRIPTION_SOURCES = tuple(
    sorted(h.value for h, b in HARNESS_BILLING.items() if b is HarnessBilling.SUBSCRIPTION)
)
_SUBSCRIPTION_SOURCES_SQL = ", ".join(repr(s) for s in SUBSCRIPTION_SOURCES)


def set_statement_timeout(target: psycopg.Connection[Any] | psycopg.Cursor[Any], ms: int) -> None:
    target.execute("SELECT set_config('statement_timeout', %s, true)", (str(ms),))


class WindowTotals(NamedTuple):
    in_total: int
    out_total: int
    cache_read: int
    cache_input: int
    cost_usd: float
    api_cost_by_model: tuple[tuple[str, Decimal], ...]


# meiva:note
# Cost is the usage-time snapshot each row carries, never today's registry price. Grouping by
# `model` returns one row per distinct model in the window so callers resolve vendor slices
# through `ModelCatalog` without duplicating provider prefixes in SQL.
# meiva:link gateway/cluster/docs/ops-surfaces.meiva.okf.md
def window_totals(
    conn: psycopg.Connection[Any], *, cluster: str, start: datetime, end: datetime
) -> WindowTotals:
    api_source = f"COALESCE({LLM_USAGE_KEYS['source']}, '') NOT IN ({_SUBSCRIPTION_SOURCES_SQL})"
    query = f"""
        SELECT
          COALESCE({LLM_USAGE_KEYS["model"]}, ''),
          COALESCE(sum({numeric(LLM_USAGE_KEYS["in_total"])}), 0),
          COALESCE(sum({numeric(LLM_USAGE_KEYS["out_total"])}), 0),
          COALESCE(sum({numeric(LLM_USAGE_KEYS["cache_read"])}), 0),
          COALESCE(sum({numeric(LLM_USAGE_KEYS["in_total"])}) FILTER (
              WHERE {numeric(LLM_USAGE_KEYS["cache_read"])} IS NOT NULL), 0),
          COALESCE(sum({numeric(LLM_USAGE_KEYS["cost_usd"])}) FILTER (WHERE {api_source}), 0)
        FROM telemetry_events
        WHERE event_name = 'llm_usage'
          AND category = 'telemetry'
          AND (cluster = %s OR cluster = '')
          AND ts > %s AND ts <= %s
        GROUP BY 1
    """  # noqa: S608
    rows = conn.execute(query, (cluster, start, end)).fetchall()  # pyright: ignore[reportArgumentType]
    in_total = sum((Decimal(row[1]) for row in rows), Decimal(0))
    out_total = sum((Decimal(row[2]) for row in rows), Decimal(0))
    cache_read = sum((Decimal(row[3]) for row in rows), Decimal(0))
    cache_input = sum((Decimal(row[4]) for row in rows), Decimal(0))
    api_costs = tuple(
        (str(row[0]), Decimal(row[5])) for row in rows if row[0] and Decimal(row[5]) != 0
    )
    total_cost = sum((Decimal(row[5]) for row in rows), Decimal(0))
    return WindowTotals(
        in_total=round(in_total),
        out_total=round(out_total),
        cache_read=round(cache_read),
        cache_input=round(cache_input),
        cost_usd=float(total_cost),
        api_cost_by_model=api_costs,
    )
