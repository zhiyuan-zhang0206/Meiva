"""The metrics window reduced in Postgres — `fetch_aggregate`, the gateway + CLI entry point.

The digest's units reduce the window's telemetry and log rows to a handful of aggregates (counts,
sums, distributions, per-key counters, position buckets). Each aggregate is computed by a few
statements over `telemetry_events` in one connection; nothing materializes the rows. Every
statement shares one scope: category telemetry or log (the table holds nothing else), the window
`[now - days, now]`, an optional single agent, and, with `since_compact`, each agent's rows
narrowed to those at or after its latest compact halt (service rows, which have no agent, are
always kept).

`EventAggregate` (in `aggregate.py`) carries the result to the assemblers, which reuse the same
helper functions (`pctiles`, `third_of`, `_fix_kinds`, `cost_usd`) as before so the math cannot
drift.

Failure events are the exec outcomes the registry names (`EXEC_FAILURE_EVENTS`); the
envelope and boot events are not outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import NamedTuple, TypedDict

import psycopg

from base.api_contracts.agent_contract import HARNESS_BILLING, HarnessBilling
from base.db.rows import query_all
from base.events.contract import (
    AGENT_SPAWNED_KEYS,
    CODE_KEYS,
    EXEC_FAILED_KEYS,
    EXEC_KEYS,
    HALT_KEYS,
    LLM_USAGE_KEYS,
    PLUGIN_ACTIVATION_KEYS,
    SYNTAX_FIX_KEYS,
    TURN_END_KEYS,
)
from base.telemetry.metrics.event_sql import numeric

EXEC_FAILURE_EVENTS = [
    "exec_failed",
    "exec_timeout",
    "exec_cancelled",
    "exec_node_timeout",
]
LIFECYCLE_EVENTS = {
    "agent_spawned": "spawned",
    "agent_resurrected": "resurrected",
}
IDLE_HALT_BODY = "no tool_call (idle)"


def _n(expression: str, cast_to: str = "numeric") -> str:
    return numeric(expression, cast_to)


# meiva:note
# One statement's row shapes. A field is named as the column the statement selects it under
# (`<expr> AS <field>`), so `class_row` builds the row by name.
@dataclass(frozen=True)
class _CutoffRow:
    agent_id: int
    cutoff: datetime


# meiva:note
# The index-only counters of one agent (agent_id None = service rows): the lifecycle fields are the
# `LIFECYCLE_EVENTS` values.
@dataclass(frozen=True)
class _CounterRow:
    agent_id: int | None
    events: int
    code: int
    turn_total: int
    exec_ok: int
    exec_failed: int
    spawned: int
    resurrected: int
    first_ts: datetime
    last_ts: datetime


# meiva:note
# Per agent: successful turns and idle halts.
@dataclass(frozen=True)
class _OutcomeRow:
    agent_id: int | None
    turn_ok: int
    idle: int


# meiva:note
# A text key and its count.
@dataclass(frozen=True)
class _NameCountRow:
    name: str
    n: int


# meiva:note
# One value of a distribution.
@dataclass(frozen=True)
class _ValueRow:
    value: float


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
    reasoning: int


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
    missing: int


# meiva:note
# Cache-read and input sums of one position third.
@dataclass(frozen=True)
class _PositionRow:
    bucket: str
    cached: int
    tin: int


# meiva:note
# One `syntax_fix` event and the code blocks its agent had written by then.
@dataclass(frozen=True)
class _FixEventRow:
    agent_id: int | None
    blk: int
    fixes: str


# meiva:note
# One (plugin, surface, identifier, model) and its activation count.
@dataclass(frozen=True)
class _PluginActRow:
    plugin: str
    surface: str
    identifier: str
    model: str
    n: int


# meiva:note
# One agent's counters over the window (key None of the window's map = service rows). A group that
# only has outcome rows reads zero everywhere else, with no first/last timestamp.
@dataclass(frozen=True)
class AgentCounters:
    events: int
    code: int
    turn_total: int
    turn_ok: int
    exec_ok: int
    exec_failed: int
    spawned: int
    resurrected: int
    idle_halts: int
    first_ts: datetime | None
    last_ts: datetime | None


# meiva:note
# Every aggregate of the metrics window, as plain Python values (`read_window`).
class WindowData(TypedDict):
    start: datetime
    end: datetime
    per_agent: dict[int | None, AgentCounters]
    failure_types: dict[str, int]
    lengths: dict[str, list[float]]
    llm_per_agent: dict[int | None, dict[str, tuple[int, int, int, int, int]]]
    llm_cache_input: dict[int | None, int]
    llm_costs: dict[int | None, dict[str, LlmCostSums]]
    llm_position: dict[str, tuple[int, int]]
    fix_events: list[tuple[int | None, int, str]]
    spawners: dict[str, int]
    plugin_acts: dict[tuple[str, str, str, str], int]


# meiva:note
# The per-agent subset of the window (`read_agent_window`).
class AgentWindowData(TypedDict):
    per_agent: dict[int | None, AgentCounters]
    llm_per_agent: dict[int | None, dict[str, tuple[int, int, int, int, int]]]
    llm_cache_input: dict[int | None, int]
    llm_costs: dict[int | None, dict[str, LlmCostSums]]


class _Scope:
    """The shared FROM/WHERE of every statement of one fetch, with its named parameters."""

    def __init__(
        self,
        start: datetime,
        end: datetime,
        agent_id: int | None,
        cutoffs: dict[int, datetime],
    ) -> None:
        self.params: dict[str, object] = {
            "start": start,
            "end": end,
            "agent": agent_id,
            "cut_agents": list(cutoffs),
            "cut_times": list(cutoffs.values()),
            "fail": EXEC_FAILURE_EVENTS,
        }
        self.from_sql = "telemetry_events t"
        self.where_sql = "t.ts >= %(start)s AND t.ts <= %(end)s"
        if agent_id is not None:
            self.where_sql += " AND t.agent_id = %(agent)s"
        if cutoffs:
            self.from_sql += (
                " LEFT JOIN unnest(%(cut_agents)s::bigint[], %(cut_times)s::timestamptz[])"
                " AS c(agent_id, cutoff) ON c.agent_id = t.agent_id"
            )
            self.where_sql += " AND (t.agent_id IS NULL OR t.ts >= COALESCE(c.cutoff, '-infinity'))"

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


def _cutoffs(
    conn: psycopg.Connection, start: datetime, end: datetime, agent_id: int | None
) -> dict[int, datetime]:
    """Each agent's latest compact halt in the window."""
    scope = _Scope(start, end, agent_id, {})
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
        "count(*) FILTER (WHERE t.event_name = 'code') AS code, "
        "count(*) FILTER (WHERE t.event_name = 'turn_end') AS turn_total, "
        "count(*) FILTER (WHERE t.event_name = 'exec') AS exec_ok, "
        "count(*) FILTER (WHERE t.event_name = ANY(%(fail)s)) AS exec_failed, "
        + ", ".join(
            f"count(*) FILTER (WHERE t.event_name = '{name}') AS {label}"
            for name, label in LIFECYCLE_EVENTS.items()
        )
        + ", min(t.ts) AS first_ts, max(t.ts) AS last_ts",
        "",
        "GROUP BY t.agent_id",
    )


def _outcomes(scope: _Scope, conn: psycopg.Connection) -> dict[int | None, tuple[int, int]]:
    """Per agent: successful turns and idle halts (both read an attribute, so they get their own
    statement over the few rows of those two events)."""
    ok = f"COALESCE({TURN_END_KEYS['ok']}, '') = 'true'"
    idle = f"COALESCE({HALT_KEYS['body']}, '') = '{IDLE_HALT_BODY}'"
    rows = scope.run(
        conn,
        _OutcomeRow,
        f"t.agent_id AS agent_id, "
        f"count(*) FILTER (WHERE t.event_name = 'turn_end' AND {ok}) AS turn_ok, "
        f"count(*) FILTER (WHERE t.event_name = 'halt' AND {idle}) AS idle",
        "t.event_name IN ('turn_end', 'halt')",
        "GROUP BY t.agent_id",
    )
    return {row.agent_id: (row.turn_ok, row.idle) for row in rows}


def _failure_types(scope: _Scope, conn: psycopg.Connection) -> dict[str, int]:
    rows = scope.run(
        conn,
        _NameCountRow,
        f"COALESCE(NULLIF({EXEC_FAILED_KEYS['exc_type']}, ''), t.event_name) AS name, "
        "count(*) AS n",
        "t.event_name = ANY(%(fail)s)",
        "GROUP BY 1 ORDER BY 2 DESC, 1",
    )
    return {row.name: row.n for row in rows}


def _lengths(scope: _Scope, conn: psycopg.Connection) -> dict[str, list[float]]:
    """The values of the three distributions, which feed `pctiles` (nearest rank in Python)."""
    out: dict[str, list[float]] = {}
    for key, expression, extra in (
        (
            "code_len",
            f"COALESCE(char_length({CODE_KEYS['body']}), 0)::float8",
            "t.event_name = 'code'",
        ),
        (
            "output_len",
            f"COALESCE(char_length({EXEC_KEYS['body']}), 0)::float8",
            "t.event_name = 'exec' OR t.event_name = ANY(%(fail)s)",
        ),
        (
            "turn_dur",
            f"COALESCE({_n(TURN_END_KEYS['duration_seconds'], 'float8')}, 0)",
            "t.event_name = 'turn_end'",
        ),
    ):
        out[key] = [
            row.value for row in scope.run(conn, _ValueRow, f"{expression} AS value", extra)
        ]
    return out


def _llm_sums(
    scope: _Scope, conn: psycopg.Connection
) -> dict[int | None, dict[str, tuple[int, int, int, int, int]]]:
    """Per (agent, model) sums of the `llm_usage` rows: calls, in, out, cached, reasoning."""
    sums = scope.run(
        conn,
        _LlmSumRow,
        f"t.agent_id AS agent_id, COALESCE({LLM_USAGE_KEYS['model']}, '') AS model, "
        f"count(*) AS calls, "
        f"COALESCE(sum({_n(LLM_USAGE_KEYS['in_total'])}), 0)::bigint AS tin, "
        f"COALESCE(sum({_n(LLM_USAGE_KEYS['out_total'])}), 0)::bigint AS tout, "
        f"COALESCE(sum({_n(LLM_USAGE_KEYS['cache_read'])}), 0)::bigint AS cached, "
        f"COALESCE(sum({_n(LLM_USAGE_KEYS['reasoning'])}), 0)::bigint AS reasoning",
        "t.event_name = 'llm_usage'",
        "GROUP BY t.agent_id, 2",
    )
    per_agent: dict[int | None, dict[str, tuple[int, int, int, int, int]]] = {}
    for row in sums:
        per_agent.setdefault(row.agent_id, {})[row.model] = (
            row.calls,
            row.tin,
            row.tout,
            row.cached,
            row.reasoning,
        )
    return per_agent


class LlmCostSums(NamedTuple):
    """Recorded USD and explicitly unpriced calls."""

    cost: float
    unpriced: int


def _llm_cache_input(scope: _Scope, conn: psycopg.Connection) -> dict[int | None, int]:
    """Input denominator from the same calls that report numeric cache reads."""
    rows = scope.run(
        conn,
        _CacheInputRow,
        f"t.agent_id AS agent_id, COALESCE(sum({_n(LLM_USAGE_KEYS['in_total'])}), 0)::bigint AS tin",
        f"t.event_name = 'llm_usage' AND {_n(LLM_USAGE_KEYS['cache_read'])} IS NOT NULL",
        "GROUP BY t.agent_id",
    )
    return {row.agent_id: row.tin for row in rows}


SUBSCRIPTION_LLM_SOURCES = tuple(
    sorted(h.value for h, b in HARNESS_BILLING.items() if b is HarnessBilling.SUBSCRIPTION)
)
_SUBSCRIPTION_SOURCES_SQL = ", ".join(repr(s) for s in SUBSCRIPTION_LLM_SOURCES)


def _llm_cost_sums(
    scope: _Scope, conn: psycopg.Connection
) -> dict[int | None, dict[str, LlmCostSums]]:
    """Recorded costs and explicitly unpriced calls per agent and model."""
    cost = _n(LLM_USAGE_KEYS["cost_usd"])
    unpriced = _n(LLM_USAGE_KEYS["unpriced"], "integer")
    cost_expr = (
        f"COALESCE(sum({cost}) FILTER (WHERE COALESCE({LLM_USAGE_KEYS['source']}, '') "
        f"NOT IN ({_SUBSCRIPTION_SOURCES_SQL})), 0)"
        if scope.params["agent"] is None
        else f"COALESCE(sum({cost}), 0)"
    )
    sums = scope.run(
        conn,
        _CostRow,
        f"t.agent_id AS agent_id, COALESCE({LLM_USAGE_KEYS['model']}, '') AS model, "
        f"{cost_expr} AS recorded, "
        f"count(*) FILTER (WHERE {cost} IS NULL AND {unpriced} = 1) AS missing",
        "t.event_name = 'llm_usage'",
        "GROUP BY t.agent_id, 2",
    )
    result: dict[int | None, dict[str, LlmCostSums]] = {}
    for row in sums:
        result.setdefault(row.agent_id, {})[row.model] = LlmCostSums(
            float(row.recorded), row.missing
        )
    return result


def _llm_position(scope: _Scope, conn: psycopg.Connection) -> dict[str, tuple[int, int]]:
    """Cache-read and input sums in the early, mid and late thirds of each agent's `llm_usage`
    rows in time order (the buckets `third_of` defines, as integer arithmetic)."""
    query = (
        "SELECT bucket, COALESCE(sum(cached), 0)::bigint AS cached, "  # noqa: S608 — keys come from the registered payload constants
        "COALESCE(sum(tin), 0)::bigint AS tin FROM ("
        "SELECT CASE WHEN 3 * ordinal < total THEN 'early' WHEN 3 * ordinal < 2 * total "
        "THEN 'mid' ELSE 'late' END AS bucket, cached, tin FROM ("
        "SELECT row_number() OVER (PARTITION BY t.agent_id ORDER BY t.ts, t.id) - 1 AS ordinal, "
        "count(*) OVER (PARTITION BY t.agent_id) AS total, "
        f"COALESCE({_n(LLM_USAGE_KEYS['cache_read'])}, 0) AS cached, "
        f"COALESCE({_n(LLM_USAGE_KEYS['in_total'])}, 0) AS tin "
        f"FROM {scope.from_sql} WHERE {scope.where_sql} AND t.event_name = 'llm_usage' "
        f"AND {_n(LLM_USAGE_KEYS['cache_read'])} IS NOT NULL) r) s "
        "GROUP BY bucket"
    )
    ranked = query_all(conn, _PositionRow, query.encode(), scope.params)
    return {row.bucket: (row.cached, row.tin) for row in ranked}


def _fix_events(scope: _Scope, conn: psycopg.Connection) -> list[tuple[int | None, int, str]]:
    """Each `syntax_fix` event with the number of code blocks its agent had written by then."""
    query = (
        "SELECT agent_id, blk, fixes FROM ("  # noqa: S608 — keys come from the registered payload constants
        "SELECT t.agent_id, t.ts, t.id, t.event_name, "
        f"CASE WHEN t.event_name = 'syntax_fix' THEN COALESCE({SYNTAX_FIX_KEYS['fixes']}, '') END "
        "AS fixes, "
        "sum((t.event_name = 'code')::int) OVER (PARTITION BY t.agent_id ORDER BY t.ts, t.id) "
        f"AS blk FROM {scope.from_sql} WHERE {scope.where_sql} "
        "AND t.event_name IN ('code', 'syntax_fix')) s "
        "WHERE event_name = 'syntax_fix' ORDER BY agent_id, ts, id"
    )
    rows = query_all(conn, _FixEventRow, query.encode(), scope.params)
    return [(row.agent_id, row.blk, row.fixes) for row in rows]


def _per_agent_counters(scope: _Scope, conn: psycopg.Connection) -> dict[int | None, AgentCounters]:
    """The window's counters per agent (key None = service rows), merged from both statements."""
    outcomes = _outcomes(scope, conn)
    per_agent: dict[int | None, AgentCounters] = {}
    for row in _per_agent(scope, conn):
        turn_ok, idle = outcomes.get(row.agent_id, (0, 0))
        per_agent[row.agent_id] = AgentCounters(
            events=row.events,
            code=row.code,
            turn_total=row.turn_total,
            turn_ok=turn_ok,
            exec_ok=row.exec_ok,
            exec_failed=row.exec_failed,
            spawned=row.spawned,
            resurrected=row.resurrected,
            idle_halts=idle,
            first_ts=row.first_ts,
            last_ts=row.last_ts,
        )
    for key, (turn_ok, idle) in outcomes.items():
        if key not in per_agent:
            per_agent[key] = AgentCounters(
                events=0,
                code=0,
                turn_total=0,
                turn_ok=turn_ok,
                exec_ok=0,
                exec_failed=0,
                spawned=0,
                resurrected=0,
                idle_halts=idle,
                first_ts=None,
                last_ts=None,
            )
    return per_agent


def read_window(
    conn: psycopg.Connection,
    days: int,
    agent_id: int | None,
    *,
    now: datetime,
    since_compact: bool = False,
) -> WindowData:
    """Every aggregate of the metrics window, as plain Python values."""
    end = now
    start = end - timedelta(days=days)
    cutoffs = _cutoffs(conn, start, end, agent_id) if since_compact else {}
    scope = _Scope(start, end, agent_id, cutoffs)

    per_agent = _per_agent_counters(scope, conn)
    llm_per_agent = _llm_sums(scope, conn)
    spawners = {
        row.name: row.n
        for row in scope.run(
            conn,
            _NameCountRow,
            f"COALESCE(NULLIF({AGENT_SPAWNED_KEYS['spawner']}, ''), '?') AS name, count(*) AS n",
            "t.event_name = 'agent_spawned'",
            "GROUP BY 1",
        )
    }
    plugin_acts = {
        (row.plugin, row.surface, row.identifier, row.model): row.n
        for row in scope.run(
            conn,
            _PluginActRow,
            f"{PLUGIN_ACTIVATION_KEYS['plugin']} AS plugin, "
            f"COALESCE({PLUGIN_ACTIVATION_KEYS['surface']}, '') AS surface, "
            f"COALESCE({PLUGIN_ACTIVATION_KEYS['identifier']}, '') AS identifier, "
            f"COALESCE({PLUGIN_ACTIVATION_KEYS['model']}, '') AS model, count(*) AS n",
            f"t.event_name = 'plugin_activation' AND COALESCE({PLUGIN_ACTIVATION_KEYS['plugin']}, '') <> ''",
            "GROUP BY 1, 2, 3, 4 ORDER BY 5 DESC, 1, 2, 3, 4",
        )
    }
    return {
        "start": start,
        "end": end,
        "per_agent": per_agent,
        "failure_types": _failure_types(scope, conn),
        "lengths": _lengths(scope, conn),
        "llm_per_agent": llm_per_agent,
        "llm_cache_input": _llm_cache_input(scope, conn),
        "llm_costs": _llm_cost_sums(scope, conn),
        "llm_position": _llm_position(scope, conn),
        "fix_events": _fix_events(scope, conn),
        "spawners": spawners,
        "plugin_acts": plugin_acts,
    }


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
    cutoffs = _cutoffs(conn, start, end, None) if since_compact else {}
    scope = _Scope(start, end, None, cutoffs)
    return {
        "per_agent": _per_agent_counters(scope, conn),
        "llm_per_agent": _llm_sums(scope, conn),
        "llm_cache_input": _llm_cache_input(scope, conn),
        "llm_costs": _llm_cost_sums(scope, conn),
    }
