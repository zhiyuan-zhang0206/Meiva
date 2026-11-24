# meiva:note
# Per-agent rollups for `/api/metrics/agents` — the gateway + CLI path.
# `fetch_agent_rollups` reads the per-agent counters from `telemetry_events` and the usage sums from
# the usage ledger (`base.telemetry.metrics.aggregate_sql`) and returns one `AgentRollup` per agent.
# meiva:link base/telemetry/metrics/docs/metrics.md

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict

import psycopg

from base.telemetry.metrics.aggregate_sql import (
    AgentCounters,
    LlmCostSums,
    read_agent_window,
)


# meiva:note
# One agent's headline counters for `/api/metrics/agents`. `cache_hit_pct` is None when the agent
# has input tokens but no call reported cache metering.
class AgentRollup(TypedDict):
    events: int
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
    turn_ok: int
    turn_total: int
    exec_ok: int
    exec_failed: int


@dataclass
class _PerAgentAgg:
    """Per-agent row aggregates — turn counts and exec outcomes.

    Includes the service-level group (agent_id None); `agent_rollups` filters it.
    """

    agent_id: int | None
    events: int
    turn_total: int
    turn_ok: int
    exec_ok: int
    exec_failed: int


def _per_agent_aggs(per_agent: dict[int | None, AgentCounters]) -> dict[int | None, _PerAgentAgg]:
    return {
        agent: _PerAgentAgg(
            agent, row.events, row.turn_total, row.turn_ok, row.exec_ok, row.exec_failed
        )
        for agent, row in per_agent.items()
        if row.events
    }


def _llm_by_agent(
    llm_per_agent: dict[int | None, dict[str, tuple[int, int, int, int]]],
) -> dict[int | None, list[tuple[str, int, int, int, int]]]:
    return {
        agent: [(model, n, i, o, c) for model, (n, i, o, c) in models.items()]
        for agent, models in llm_per_agent.items()
    }


def fetch_agent_rollups(
    conn: psycopg.Connection,
    days: int,
    *,
    now: datetime,
    since_compact: bool = False,
) -> tuple[int, dict[int, AgentRollup]]:
    """`(total_events, per-agent headline counters)` of the window, for `/api/metrics/agents`.

    Runs only the statements that endpoint needs: the per-agent counters and the `llm_usage` sums.
    """
    data = read_agent_window(conn, days, now=now, since_compact=since_compact)
    raw = data["per_agent"]
    per_agent = _per_agent_aggs(raw)
    total = sum(r.events for r in raw.values())
    return total, agent_rollups(
        per_agent, _llm_by_agent(data["llm_per_agent"]), data["llm_costs"], data["llm_cache_input"]
    )


def _cost_totals(rows: Iterable[LlmCostSums]) -> LlmCostSums:
    cost, subscription = 0.0, 0.0
    unpriced, incomplete = 0, 0
    for row in rows:
        cost += row.cost
        subscription += row.subscription
        unpriced += row.unpriced
        incomplete += row.incomplete
    return LlmCostSums(cost, unpriced, subscription, incomplete)


def agent_rollups(
    per_agent: dict[int | None, _PerAgentAgg],
    per_agent_llm: dict[int | None, list[tuple[str, int, int, int, int]]],
    costs: dict[int | None, dict[str, LlmCostSums]],
    cache_inputs: dict[int | None, int],
) -> dict[int, AgentRollup]:
    """Per-agent headline counters for `/api/metrics/agents` (service rows excluded)."""
    out: dict[int, AgentRollup] = {}
    for aid in per_agent.keys() | per_agent_llm.keys():
        if aid is None:
            continue
        usage = per_agent_llm.get(aid, [])
        calls = sum(r[1] for r in usage)
        tin = sum(r[2] for r in usage)
        tout = sum(r[3] for r in usage)
        tcached = sum(r[4] for r in usage)
        cache_input = cache_inputs.get(aid, 0)
        cost = _cost_totals(costs.get(aid, {}).values())
        row = per_agent.get(aid)
        out[aid] = {
            "events": row.events if row is not None else 0,
            "cost_usd": round(cost.cost, 4),
            "cost_usd_api": round(max(0.0, cost.cost - cost.subscription), 4),
            "cost_usd_subscription": round(cost.subscription, 4),
            "usage_incomplete_reports": cost.incomplete,
            "usage_unpriced_calls": cost.unpriced,
            "llm_calls": calls,
            "tokens_in": tin,
            "tokens_out": tout,
            "tokens_cached": tcached,
            "cache_hit_pct": round(tcached / cache_input * 100, 1)
            if cache_input
            else (None if tin else 0.0),
            "turn_ok": row.turn_ok if row is not None else 0,
            "turn_total": row.turn_total if row is not None else 0,
            "exec_ok": row.exec_ok if row is not None else 0,
            "exec_failed": row.exec_failed if row is not None else 0,
        }
    return out
