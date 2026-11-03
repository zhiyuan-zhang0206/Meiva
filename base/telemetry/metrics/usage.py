"""Selectable agent usage scopes over durable events and the lifetime ledger."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time
from decimal import Decimal
from typing import Literal, TypedDict

import psycopg

from base.db.rows import IdRow, query_all, query_one
from base.events.contract import LLM_USAGE_KEYS
from base.telemetry.metrics.event_sql import numeric

# One grouped row per (agent, model): tokens in/out/cached/reasoning, calls,
# summed cost snapshots, unpriced calls. The aggregate() contract.
_Row = tuple[int, str, int, int, int, int, int, float, int]


_COST = LLM_USAGE_KEYS["cost_usd"]

# Per (agent, model) llm_usage aggregates over [from, to) with one tail start per agent:
# one sum per token/cost field, one count, one count of rows without a cost snapshot.
_EVENT_SQL = f"""
    SELECT t.agent_id AS agent_id, COALESCE({LLM_USAGE_KEYS["model"]}, '') AS model,
           COALESCE(sum({numeric(LLM_USAGE_KEYS["in_total"])}), 0)::bigint AS tokens_in,
           COALESCE(sum({numeric(LLM_USAGE_KEYS["out_total"])}), 0)::bigint AS tokens_out,
           COALESCE(sum({numeric(LLM_USAGE_KEYS["cache_read"])}), 0)::bigint AS tokens_cached,
           COALESCE(sum({numeric(LLM_USAGE_KEYS["reasoning"])}), 0)::bigint AS tokens_reasoning,
           count(*) AS llm_calls,
           COALESCE(sum({numeric(LLM_USAGE_KEYS["cost_usd"])}), 0)::float8 AS cost_usd,
           count(*) FILTER (WHERE ({numeric(_COST)}) IS NULL) AS unpriced_calls
    FROM telemetry_events t
    JOIN unnest(%s::bigint[], %s::timestamptz[]) AS w(agent_id, tail_from)
      ON w.agent_id = t.agent_id
    WHERE t.event_name = 'llm_usage' AND t.ts >= w.tail_from AND t.ts < %s
    GROUP BY t.agent_id, 2
"""  # noqa: S608 — keys come from the registered payload constants

# The ledger's whole-life sums come in two parts: the folded totals up to the watermark of
# `agent_model_tokens_total_through`, and the ledger days after it. Both group per (agent, model).
_THROUGH_SQL = "SELECT day FROM agent_model_tokens_total_through"

_TOTAL_SQL = """SELECT agent_id, model,
  llm_calls, costed_calls, unpriced_calls,
  tokens_in, tokens_out, tokens_cached, tokens_reasoning,
  cost_usd
FROM agent_model_tokens_total
WHERE agent_id = ANY(%s)"""

_LEDGER_SQL = """SELECT agent_id, model,
  sum(llm_calls) AS llm_calls, sum(costed_calls) AS costed_calls,
  sum(unpriced_calls) AS unpriced_calls,
  sum(tokens_in) AS tokens_in, sum(tokens_out) AS tokens_out,
  sum(tokens_cached) AS tokens_cached, sum(tokens_reasoning) AS tokens_reasoning,
  sum(cost_usd) AS cost_usd
FROM agent_model_tokens_daily
WHERE agent_id = ANY(%s) AND day > %s
GROUP BY agent_id, model"""

_NEWEST_LEDGER_DAYS_SQL = """SELECT agent_id, max(day) AS day
FROM agent_model_tokens_daily
WHERE agent_id = ANY(%s) AND day > %s
GROUP BY agent_id"""

_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


# meiva:note
# One `_EVENT_SQL` row: the column names are the SQL aliases.
@dataclass(frozen=True)
class _EventRow:
    agent_id: int
    model: str
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    tokens_reasoning: int
    llm_calls: int
    cost_usd: float
    unpriced_calls: int


# meiva:note
# One `_TOTAL_SQL` row: the folded whole-life sums per (agent, model), stored as bigint columns.
@dataclass(frozen=True)
class _TotalRow:
    agent_id: int
    model: str
    llm_calls: int
    costed_calls: int
    unpriced_calls: int
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    tokens_reasoning: int
    cost_usd: float


# meiva:note
# One `_LEDGER_SQL` row: `sum()` of a bigint column is numeric, so the counters arrive as Decimal;
# `cost_usd` is a double precision sum and stays a float.
@dataclass(frozen=True)
class _LedgerRow:
    agent_id: int
    model: str
    llm_calls: Decimal
    costed_calls: Decimal
    unpriced_calls: Decimal
    tokens_in: Decimal
    tokens_out: Decimal
    tokens_cached: Decimal
    tokens_reasoning: Decimal
    cost_usd: float


# meiva:note
# The `_THROUGH_SQL` answer: the watermark day of the folded totals.
@dataclass(frozen=True)
class _ThroughRow:
    day: date


# meiva:note
# One `_NEWEST_LEDGER_DAYS_SQL` row: an agent and its newest ledger day.
@dataclass(frozen=True)
class _NewestDayRow:
    agent_id: int
    day: date


def event_rows(conn: psycopg.Connection, tails: dict[int, datetime], until: datetime) -> list[_Row]:
    """The `llm_usage` rows of `telemetry_events` from each agent's tail start to `until`."""
    if not tails:
        return []
    agent_ids = sorted(tails)
    rows = query_all(
        conn,
        _EventRow,
        _EVENT_SQL.encode(),
        (agent_ids, [tails[a] for a in agent_ids], until),
    )
    return [
        (
            r.agent_id,
            r.model,
            r.tokens_in,
            r.tokens_out,
            r.tokens_cached,
            r.tokens_reasoning,
            r.llm_calls,
            r.cost_usd,
            r.unpriced_calls,
        )
        for r in rows
    ]


def ledger_rows(
    conn: psycopg.Connection, agent_ids: list[int]
) -> tuple[list[_Row], dict[int, datetime]]:
    """Ledger rows (folded totals + the days after the watermark) and each agent's live-tail start:
    the midnight after its newest ledger day, the midnight after the watermark for an agent whose
    ledger is folded entirely, and the beginning for an agent with no ledger row."""
    through_row = query_one(conn, _ThroughRow, _THROUGH_SQL)
    through = through_row.day if through_row is not None else date.min
    rows: list[_Row] = []
    tails: dict[int, datetime] = dict.fromkeys(agent_ids, _EPOCH)
    folded_tail = datetime.combine(through + timedelta(days=1), day_time.min, tzinfo=UTC)
    for total in query_all(conn, _TotalRow, _TOTAL_SQL.encode(), (agent_ids,)):
        rows.append(
            (
                total.agent_id,
                total.model,
                total.tokens_in,
                total.tokens_out,
                total.tokens_cached,
                total.tokens_reasoning,
                total.llm_calls,
                total.cost_usd,
                total.unpriced_calls,
            )
        )
        tails[total.agent_id] = folded_tail
    for ledger in query_all(conn, _LedgerRow, _LEDGER_SQL.encode(), (agent_ids, through)):
        rows.append(
            (
                ledger.agent_id,
                ledger.model,
                int(ledger.tokens_in),
                int(ledger.tokens_out),
                int(ledger.tokens_cached),
                int(ledger.tokens_reasoning),
                int(ledger.llm_calls),
                ledger.cost_usd,
                int(ledger.unpriced_calls),
            )
        )
    for newest in query_all(conn, _NewestDayRow, _NEWEST_LEDGER_DAYS_SQL, (agent_ids, through)):
        tails[newest.agent_id] = datetime.combine(
            newest.day + timedelta(days=1), day_time.min, tzinfo=UTC
        )
    return rows, tails


# meiva:note
# The folded usage of one model under one agent; `cost_usd` is None until a costed call lands.
class ModelUsage(TypedDict):
    cost_usd: float | None
    llm_calls: int
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    tokens_reasoning: int


# meiva:note
# The folded usage of one agent over all of its models.
class AgentUsage(TypedDict):
    cost_usd: float
    llm_calls: int
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    tokens_reasoning: int
    unpriced_calls: int
    by_model: dict[str, ModelUsage]


# meiva:note
# The sums over every agent of an aggregate, and how many agents there were.
class UsageSums(TypedDict):
    cost_usd: float
    llm_calls: int
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    tokens_reasoning: int
    unpriced_calls: int
    distinct_agents: int


# meiva:note
# What `aggregate` answers: the folded usage per agent id (as text) and the sums over all agents.
class Aggregate(TypedDict):
    per_agent: dict[str, AgentUsage]
    total: UsageSums


def _new_model_usage() -> ModelUsage:
    return {
        "cost_usd": None,
        "llm_calls": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_cached": 0,
        "tokens_reasoning": 0,
    }


def _new_agent_usage() -> AgentUsage:
    return {
        "cost_usd": 0.0,
        "llm_calls": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_cached": 0,
        "tokens_reasoning": 0,
        "unpriced_calls": 0,
        "by_model": {},
    }


def aggregate(rows: list[_Row]) -> Aggregate:
    """Fold the grouped rows into `{per_agent: {id: {...}}, total: {...}}`.

    Pure — no DB — so the folding math is unit-tested directly. Per
    (agent, model) the cost is the summed usage-time snapshot; a model with
    no costed call reports `cost_usd: None` in `by_model` (never silently
    $0). Reports retain precision for budget comparisons."""
    per_agent: dict[str, AgentUsage] = {}
    for aid, model, r_in, r_out, r_cached, r_reason, r_calls, r_cost, r_unpriced in rows:
        a = per_agent.setdefault(str(aid), _new_agent_usage())
        model_usage = a["by_model"].setdefault(model, _new_model_usage())
        if r_calls > r_unpriced:
            model_usage["cost_usd"] = (model_usage["cost_usd"] or 0.0) + r_cost
        model_usage["llm_calls"] += r_calls
        model_usage["tokens_in"] += r_in
        model_usage["tokens_out"] += r_out
        model_usage["tokens_cached"] += r_cached
        model_usage["tokens_reasoning"] += r_reason
        a["cost_usd"] += r_cost
        a["llm_calls"] += r_calls
        a["tokens_in"] += r_in
        a["tokens_out"] += r_out
        a["tokens_cached"] += r_cached
        a["tokens_reasoning"] += r_reason
        a["unpriced_calls"] += r_unpriced

    total: UsageSums = {
        "cost_usd": 0.0,
        "llm_calls": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_cached": 0,
        "tokens_reasoning": 0,
        "unpriced_calls": 0,
        "distinct_agents": len(per_agent),
    }
    for a in per_agent.values():
        total["llm_calls"] += a["llm_calls"]
        total["tokens_in"] += a["tokens_in"]
        total["tokens_out"] += a["tokens_out"]
        total["tokens_cached"] += a["tokens_cached"]
        total["tokens_reasoning"] += a["tokens_reasoning"]
        total["unpriced_calls"] += a["unpriced_calls"]
        total["cost_usd"] += a["cost_usd"]
    return {"per_agent": per_agent, "total": total}


Lineage = Literal["self", "spawn", "fork", "all"]


def select_agents(conn: psycopg.Connection, roots: Sequence[int], lineage: Lineage) -> list[int]:
    """Include roots and recursively traverse only the selected birth edges.

    Spawn edges exclude forks. Fork edges follow the context source, not the
    caller who requested a fork. `all` traverses either edge, including mixed
    chains. Immutable birth ancestry survives later tree folding. UNION prevents
    overlapping roots and historical cycles from counting an agent twice.
    """
    if lineage not in {"self", "spawn", "fork", "all"}:
        raise ValueError(f"unknown lineage: {lineage!r}")
    if not roots or any(type(root) is not int or root <= 0 for root in roots):
        raise ValueError("roots must contain positive integer agent IDs")
    unique_roots = sorted(set(roots))
    found = {
        row.id
        for row in query_all(
            conn, IdRow, "SELECT id FROM agents_meta WHERE id = ANY(%s)", (unique_roots,)
        )
    }
    missing = set(unique_roots) - found
    if missing:
        raise ValueError(f"unknown agent IDs: {sorted(missing)}")
    if lineage == "self":
        return unique_roots
    rows = query_all(
        conn,
        IdRow,
        """WITH RECURSIVE selected(id) AS (
            SELECT id FROM agents_meta WHERE id = ANY(%s)
            UNION
            SELECT child.id FROM agents_meta child JOIN selected parent ON (
                (%s IN ('spawn', 'all') AND child.fork_source_agent_id IS NULL
                 AND child.born_spawner = 'agent:' || parent.id::text)
                OR (%s IN ('fork', 'all') AND child.fork_source_agent_id = parent.id)
            )
        ) SELECT id FROM selected ORDER BY id""",
        (unique_roots, lineage, lineage),
    )
    return [row.id for row in rows]


# meiva:note
# One agent of a usage report; the keys are the report's public field names.
class AgentUsageReport(TypedDict):
    agent_id: int
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    reasoning_tokens: int
    total_tokens: int
    recorded_cost_usd: float
    unpriced_calls: int
    by_model: dict[str, ModelUsage]


# meiva:note
# The sums over every agent of a usage report.
class UsageReportTotals(TypedDict):
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    reasoning_tokens: int
    total_tokens: int
    recorded_cost_usd: float
    unpriced_calls: int


# meiva:note
# What `usage_report` answers; it is serialised to JSON by its consumers.
class UsageReport(TypedDict):
    roots: list[int]
    lineage: Lineage
    start: str | None
    end: str
    agents: list[AgentUsageReport]
    totals: UsageReportTotals
    coverage: str


# meiva:note
# One agent's line of the usage report from its folded usage.
def _agent_report(agent_id: int, row: AgentUsage) -> AgentUsageReport:
    return {
        "agent_id": agent_id,
        "calls": row["llm_calls"],
        "input_tokens": row["tokens_in"],
        "output_tokens": row["tokens_out"],
        "cache_read_tokens": row["tokens_cached"],
        "reasoning_tokens": row["tokens_reasoning"],
        "total_tokens": row["tokens_in"] + row["tokens_out"],
        "recorded_cost_usd": row["cost_usd"],
        "unpriced_calls": row["unpriced_calls"],
        "by_model": row["by_model"],
    }


# meiva:note
# The totals of the report: the sum of every agent's line.
def _report_totals(per_agent: Sequence[AgentUsageReport]) -> UsageReportTotals:
    return {
        "calls": sum(agent["calls"] for agent in per_agent),
        "input_tokens": sum(agent["input_tokens"] for agent in per_agent),
        "output_tokens": sum(agent["output_tokens"] for agent in per_agent),
        "cache_read_tokens": sum(agent["cache_read_tokens"] for agent in per_agent),
        "reasoning_tokens": sum(agent["reasoning_tokens"] for agent in per_agent),
        "total_tokens": sum(agent["total_tokens"] for agent in per_agent),
        "recorded_cost_usd": sum(agent["recorded_cost_usd"] for agent in per_agent),
        "unpriced_calls": sum(agent["unpriced_calls"] for agent in per_agent),
    }


def usage_report(
    conn: psycopg.Connection,
    *,
    roots: Sequence[int],
    lineage: Lineage,
    start: datetime | None,
    end: datetime,
) -> UsageReport:
    """Report recorded usage in [start, end), or lifetime ledger + tail.

    Lifetime totals use folded history and must end at the current time. Missing
    prices remain unpriced_calls; in-flight calls and other expenses are absent.
    """
    if end.utcoffset() is None or (
        start is not None and (start.utcoffset() is None or start >= end)
    ):
        raise ValueError("window must have timezone-aware timestamps with start < end")
    agents = select_agents(conn, roots, lineage)
    if start is None:
        rows, tails = ledger_rows(conn, agents)
        rows.extend(event_rows(conn, tails, end))
    else:
        rows = event_rows(conn, dict.fromkeys(agents, start), end)
    folded = aggregate(rows)
    per_agent = [
        _agent_report(agent_id, folded["per_agent"].get(str(agent_id)) or _new_agent_usage())
        for agent_id in agents
    ]
    totals = _report_totals(per_agent)
    return {
        "roots": sorted(set(roots)),
        "lineage": lineage,
        "start": start.isoformat() if start is not None else None,
        "end": end.isoformat(),
        "agents": per_agent,
        "totals": totals,
        "coverage": "Recorded LLM usage only; windows use retained events, lifetime uses ledger + tail. Excludes in-flight/unreported usage and other expenses.",
    }
