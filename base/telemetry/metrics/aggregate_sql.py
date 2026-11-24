"""The per-agent metrics window reduced in Postgres — `read_agent_window`, behind `/api/metrics/agents`.

The per-agent counters (events, turn and exec outcomes) are a few statements over
`telemetry_events` in one connection; nothing materializes the rows. Usage statements read the
usage ledger. Every statement shares one scope: category telemetry or log (the table holds nothing
else), the window `[now - days, now]`, and, with `since_compact`, each agent's rows narrowed to
those at or after its latest compact halt (service rows, which have no agent, are always kept).

Failure events are the exec outcomes the registry names (`EXEC_FAILURE_EVENTS`); the
envelope and boot events are not outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import NamedTuple, TypedDict

import psycopg

from base.db.rows import query_all
from base.events.contract import HALT_KEYS, TURN_END_KEYS

EXEC_FAILURE_EVENTS = [
    "exec_failed",
    "exec_timeout",
    "exec_cancelled",
    "exec_node_timeout",
]


# meiva:note
# One statement's row shapes. A field is named as the column the statement selects it under
# (`<expr> AS <field>`), so `class_row` builds the row by name.
@dataclass(frozen=True)
class _CutoffRow:
    agent_id: int
    cutoff: datetime


# meiva:note
# The index-only counters of one agent (agent_id None = service rows).
@dataclass(frozen=True)
class _CounterRow:
    agent_id: int | None
    events: int
    turn_total: int
    exec_ok: int
    exec_failed: int


# meiva:note
# Per agent: successful turns.
@dataclass(frozen=True)
class _OutcomeRow:
    agent_id: int | None
    turn_ok: int


# meiva:note
# Per (agent, model) `llm_usage` sums.
@dataclass(frozen=True)
class _LlmSumRow:
    agent_id: int | None
    model: str
    calls: int
    tin: int
    tout: int
    cached: int


# meiva:note
# Per agent input tokens of the calls that report cache reads.
@dataclass(frozen=True)
class _CacheInputRow:
    agent_id: int | None
    tin: int


# meiva:note
# Per (agent, model) recorded USD (numeric) and unpriced call count.
@dataclass(frozen=True)
class _CostRow:
    agent_id: int | None
    model: str
    recorded: Decimal
    subscription: Decimal
    missing: int
    incomplete: int


# meiva:note
# One agent's counters over the window (key None of the window's map = service rows). A group that
# only has outcome rows reads zero everywhere else.
@dataclass(frozen=True)
class AgentCounters:
    events: int
    turn_total: int
    turn_ok: int
    exec_ok: int
    exec_failed: int


# meiva:note
# The per-agent aggregates of the window (`read_agent_window`).
class AgentWindowData(TypedDict):
    per_agent: dict[int | None, AgentCounters]
    llm_per_agent: dict[int | None, dict[str, tuple[int, int, int, int]]]
    llm_cache_input: dict[int | None, int]
    llm_costs: dict[int | None, dict[str, LlmCostSums]]


class _Scope:
    """The shared FROM/WHERE of every statement of one fetch, with its named parameters."""

    def __init__(
        self,
        start: datetime,
        end: datetime,
        cutoffs: dict[int, datetime],
        *,
        usage: bool = False,
    ) -> None:
        self.params: dict[str, object] = {
            "start": start,
            "end": end,
            "cut_agents": list(cutoffs),
            "cut_times": list(cutoffs.values()),
            "fail": EXEC_FAILURE_EVENTS,
        }
        self.from_sql = "llm_usage_ledger t" if usage else "telemetry_events t"
        timestamp = "t.occurred_at" if usage else "t.ts"
        end_operator = "<" if usage else "<="
        self.where_sql = f"{timestamp} >= %(start)s AND {timestamp} {end_operator} %(end)s"
        if cutoffs:
            self.from_sql += (
                " LEFT JOIN unnest(%(cut_agents)s::bigint[], %(cut_times)s::timestamptz[])"
                " AS c(agent_id, cutoff) ON c.agent_id = t.agent_id"
            )
            self.where_sql += (
                f" AND (t.agent_id IS NULL OR {timestamp} >= COALESCE(c.cutoff, '-infinity'))"
            )

    # meiva:note
    # `SELECT <select> FROM <scope> WHERE <scope> [AND extra] <tail>` as `row_class` rows.
    def run[RowT](
        self,
        conn: psycopg.Connection,
        row_class: type[RowT],
        select: str,
        extra: str = "",
        tail: str = "",
    ) -> list[RowT]:
        where = f"{self.where_sql} AND ({extra})" if extra else self.where_sql
        query = f"SELECT {select} FROM {self.from_sql} WHERE {where} {tail}"  # noqa: S608
        return query_all(conn, row_class, query.encode(), self.params)


def _cutoffs(conn: psycopg.Connection, start: datetime, end: datetime) -> dict[int, datetime]:
    """Each agent's latest compact halt in the window."""
    scope = _Scope(start, end, {})
    rows = scope.run(
        conn,
        _CutoffRow,
        "t.agent_id AS agent_id, max(t.ts) AS cutoff",
        f"t.event_name = 'halt' AND t.agent_id IS NOT NULL AND {HALT_KEYS['body']} LIKE '%%compact%%'",
        "GROUP BY t.agent_id",
    )
    return {row.agent_id: row.cutoff for row in rows}


def _per_agent(scope: _Scope, conn: psycopg.Connection) -> list[_CounterRow]:
    """Counts per agent (and for service rows, agent NULL) from columns the index carries."""
    return scope.run(
        conn,
        _CounterRow,
        "t.agent_id AS agent_id, count(*) AS events, "
        "count(*) FILTER (WHERE t.event_name = 'turn_end') AS turn_total, "
        "count(*) FILTER (WHERE t.event_name = 'exec') AS exec_ok, "
        "count(*) FILTER (WHERE t.event_name = ANY(%(fail)s)) AS exec_failed",
        "",
        "GROUP BY t.agent_id",
    )


def _outcomes(scope: _Scope, conn: psycopg.Connection) -> dict[int | None, int]:
    """Per agent: successful turns (the outcome reads an attribute, so it gets its own statement
    over the few rows of that one event)."""
    ok = f"COALESCE({TURN_END_KEYS['ok']}, '') = 'true'"
    rows = scope.run(
        conn,
        _OutcomeRow,
        f"t.agent_id AS agent_id, count(*) FILTER (WHERE {ok}) AS turn_ok",
        "t.event_name = 'turn_end'",
        "GROUP BY t.agent_id",
    )
    return {row.agent_id: row.turn_ok for row in rows}


def _llm_sums(
    scope: _Scope, conn: psycopg.Connection
) -> dict[int | None, dict[str, tuple[int, int, int, int]]]:
    """Per (agent, model) sums of the `llm_usage` rows: calls, in, out, cached."""
    sums = scope.run(
        conn,
        _LlmSumRow,
        "t.agent_id, t.model, sum(t.calls)::bigint AS calls, "
        "COALESCE(sum(t.tokens_in), 0)::bigint AS tin, "
        "COALESCE(sum(t.tokens_out), 0)::bigint AS tout, "
        "COALESCE(sum(t.cache_read), 0)::bigint AS cached",
        tail="GROUP BY t.agent_id, t.model",
    )
    per_agent: dict[int | None, dict[str, tuple[int, int, int, int]]] = {}
    for row in sums:
        per_agent.setdefault(row.agent_id, {})[row.model] = (
            row.calls,
            row.tin,
            row.tout,
            row.cached,
        )
    return per_agent


class LlmCostSums(NamedTuple):
    """Recorded USD and explicitly unpriced calls."""

    cost: float
    unpriced: int
    subscription: float = 0
    incomplete: int = 0


def _llm_cache_input(scope: _Scope, conn: psycopg.Connection) -> dict[int | None, int]:
    """Input denominator from the same calls that report numeric cache reads."""
    rows = scope.run(
        conn,
        _CacheInputRow,
        "t.agent_id, COALESCE(sum(t.tokens_in), 0)::bigint AS tin",
        "t.cache_read IS NOT NULL",
        "GROUP BY t.agent_id",
    )
    return {row.agent_id: row.tin for row in rows}


def _llm_cost_sums(
    scope: _Scope, conn: psycopg.Connection
) -> dict[int | None, dict[str, LlmCostSums]]:
    sums = scope.run(
        conn,
        _CostRow,
        "t.agent_id, t.model, COALESCE(sum(t.cost_usd), 0) AS recorded, "
        "COALESCE(sum(t.cost_usd) FILTER (WHERE t.billing = 'subscription'), 0) AS subscription, "
        "COALESCE(sum(t.calls) FILTER (WHERE t.cost_usd IS NULL), 0)::bigint AS missing, "
        "count(*) FILTER (WHERE NOT t.complete) AS incomplete",
        tail="GROUP BY t.agent_id, t.model",
    )
    result: dict[int | None, dict[str, LlmCostSums]] = {}
    for row in sums:
        result.setdefault(row.agent_id, {})[row.model] = LlmCostSums(
            float(row.recorded), row.missing, float(row.subscription), row.incomplete
        )
    return result


def _per_agent_counters(scope: _Scope, conn: psycopg.Connection) -> dict[int | None, AgentCounters]:
    """The window's counters per agent (key None = service rows), merged from both statements."""
    outcomes = _outcomes(scope, conn)
    per_agent: dict[int | None, AgentCounters] = {}
    for row in _per_agent(scope, conn):
        per_agent[row.agent_id] = AgentCounters(
            events=row.events,
            turn_total=row.turn_total,
            turn_ok=outcomes.get(row.agent_id, 0),
            exec_ok=row.exec_ok,
            exec_failed=row.exec_failed,
        )
    for key, turn_ok in outcomes.items():
        if key not in per_agent:
            per_agent[key] = AgentCounters(
                events=0, turn_total=0, turn_ok=turn_ok, exec_ok=0, exec_failed=0
            )
    return per_agent


def read_agent_window(
    conn: psycopg.Connection,
    days: int,
    *,
    now: datetime,
    since_compact: bool = False,
) -> AgentWindowData:
    """The per-agent counters and `llm_usage` sums of the window, for `/api/metrics/agents`."""
    end = now
    start = end - timedelta(days=days)
    cutoffs = _cutoffs(conn, start, end) if since_compact else {}
    scope = _Scope(start, end, cutoffs)
    usage = _Scope(start, end, cutoffs, usage=True)
    return {
        "per_agent": _per_agent_counters(scope, conn),
        "llm_per_agent": _llm_sums(usage, conn),
        "llm_cache_input": _llm_cache_input(usage, conn),
        "llm_costs": _llm_cost_sums(usage, conn),
    }
