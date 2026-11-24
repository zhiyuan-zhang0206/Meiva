# meiva:link gateway/cluster/docs/ops-surfaces.md

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import NamedTuple

import psycopg

from base.db.rows import query_all


def set_statement_timeout[RowT](
    target: psycopg.Connection[RowT] | psycopg.Cursor[RowT], ms: int
) -> None:
    target.execute("SELECT set_config('statement_timeout', %s, true)", (str(ms),))


# meiva:note
# One model's ledger totals over the window; psycopg hands back `Decimal` for a numeric sum and
# a `float` when the payload value was stored as a double, so each is read through `Decimal(...)`.
@dataclass(frozen=True)
class _ModelUsageRow:
    model: str
    in_total: Decimal | float
    out_total: Decimal | float
    cache_read: Decimal | float
    cache_input: Decimal | float
    cost_usd: Decimal | float
    subscription_cost_usd: Decimal | float
    unpriced_calls: int
    incomplete_reports: int


class WindowTotals(NamedTuple):
    in_total: int
    out_total: int
    cache_read: int
    cache_input: int
    cost_usd: float
    subscription_cost_usd: float
    api_cost_by_model: tuple[tuple[str, Decimal], ...]
    unpriced_calls: int = 0
    incomplete_reports: int = 0


# meiva:note
# Cost uses the usage-time snapshot each row carries. Grouping by
# `model` returns one row per distinct model in the window so callers resolve vendor slices
# through `ModelCatalog` without duplicating provider prefixes in SQL.
# meiva:link gateway/cluster/docs/ops-surfaces.md
def window_totals(
    conn: psycopg.Connection, *, cluster: str, start: datetime, end: datetime
) -> WindowTotals:
    query = """
        SELECT model,
          COALESCE(sum(tokens_in), 0) AS in_total,
          COALESCE(sum(tokens_out), 0) AS out_total,
          COALESCE(sum(cache_read), 0) AS cache_read,
          COALESCE(sum(tokens_in) FILTER (WHERE cache_read IS NOT NULL), 0) AS cache_input,
          COALESCE(sum(cost_usd) FILTER (WHERE billing = 'api'), 0) AS cost_usd,
          COALESCE(sum(cost_usd) FILTER (WHERE billing = 'subscription'), 0) AS subscription_cost_usd,
          COALESCE(sum(calls) FILTER (WHERE cost_usd IS NULL), 0)::bigint AS unpriced_calls,
          count(*) FILTER (WHERE NOT complete) AS incomplete_reports
        FROM llm_usage_ledger
        WHERE (cluster = %s OR cluster = '')
          AND occurred_at >= %s AND occurred_at < %s
        GROUP BY model
    """
    rows = query_all(conn, _ModelUsageRow, query, (cluster, start, end))  # pyright: ignore[reportArgumentType]
    in_total = sum((Decimal(row.in_total) for row in rows), Decimal(0))
    out_total = sum((Decimal(row.out_total) for row in rows), Decimal(0))
    cache_read = sum((Decimal(row.cache_read) for row in rows), Decimal(0))
    cache_input = sum((Decimal(row.cache_input) for row in rows), Decimal(0))
    api_costs = tuple(
        (row.model, Decimal(row.cost_usd))
        for row in rows
        if row.model and Decimal(row.cost_usd) != 0
    )
    total_cost = sum((Decimal(row.cost_usd) for row in rows), Decimal(0))
    subscription_cost = sum((Decimal(row.subscription_cost_usd) for row in rows), Decimal(0))
    return WindowTotals(
        in_total=round(in_total),
        out_total=round(out_total),
        cache_read=round(cache_read),
        cache_input=round(cache_input),
        cost_usd=float(total_cost),
        subscription_cost_usd=float(subscription_cost),
        api_cost_by_model=api_costs,
        unpriced_calls=sum(row.unpriced_calls for row in rows),
        incomplete_reports=sum(row.incomplete_reports for row in rows),
    )
