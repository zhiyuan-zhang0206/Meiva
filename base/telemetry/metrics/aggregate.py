# meiva:note
# Per-agent usage rollups for `/api/metrics/agents` — the gateway + CLI path.
# `fetch_agent_rollups` reads the usage ledger (`llm_usage_ledger`) in one statement and returns one
# `AgentRollup` per agent that has usage in the window.
# meiva:link base/telemetry/metrics/docs/metrics.md

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TypedDict

import psycopg

from base.db.rows import query_all


# meiva:note
# One agent's headline usage for `/api/metrics/agents`. `cache_hit_pct` is None when the agent
# has input tokens but no call reported cache metering.
class AgentRollup(TypedDict):
    cost_usd: float
    cost_usd_api: float
    cost_usd_subscription: float
    usage_incomplete_reports: int
    usage_unpriced_calls: int
    llm_calls: int
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    cache_hit_pct: float | None


@dataclass(frozen=True)
class _UsageRow:
    agent_id: int
    calls: int
    tin: int
    tout: int
    cached: int
    cache_input: int
    recorded: Decimal
    subscription: Decimal
    missing: int
    incomplete: int


_USAGE = """
SELECT agent_id, COALESCE(sum(calls), 0)::bigint AS calls,
  COALESCE(sum(tokens_in), 0)::bigint AS tin,
  COALESCE(sum(tokens_out), 0)::bigint AS tout,
  COALESCE(sum(cache_read), 0)::bigint AS cached,
  COALESCE(sum(tokens_in) FILTER (WHERE cache_read IS NOT NULL), 0)::bigint AS cache_input,
  COALESCE(sum(cost_usd), 0) AS recorded,
  COALESCE(sum(cost_usd) FILTER (WHERE billing = 'subscription'), 0) AS subscription,
  COALESCE(sum(calls) FILTER (WHERE cost_usd IS NULL), 0)::bigint AS missing,
  count(*) FILTER (WHERE NOT complete) AS incomplete
FROM llm_usage_ledger
WHERE occurred_at >= %(start)s AND occurred_at < %(end)s AND agent_id IS NOT NULL
GROUP BY agent_id
"""


def fetch_agent_rollups(
    conn: psycopg.Connection, days: int, *, now: datetime
) -> dict[int, AgentRollup]:
    """Per-agent headline usage over `[now - days, now)`; service rows (no agent) are excluded."""
    rows = query_all(conn, _UsageRow, _USAGE, {"start": now - timedelta(days=days), "end": now})
    out: dict[int, AgentRollup] = {}
    for row in rows:
        recorded = float(row.recorded)
        subscription = float(row.subscription)
        out[row.agent_id] = {
            "cost_usd": round(recorded, 4),
            "cost_usd_api": round(max(0.0, recorded - subscription), 4),
            "cost_usd_subscription": round(subscription, 4),
            "usage_incomplete_reports": row.incomplete,
            "usage_unpriced_calls": row.missing,
            "llm_calls": row.calls,
            "tokens_in": row.tin,
            "tokens_out": row.tout,
            "tokens_cached": row.cached,
            "cache_hit_pct": round(row.cached / row.cache_input * 100, 1)
            if row.cache_input
            else (None if row.tin else 0.0),
        }
    return out
