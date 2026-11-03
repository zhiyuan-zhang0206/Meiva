"""Aggregate fetch for metrics — the gateway + CLI path.

The digest's units reduce the window's telemetry and log rows to a handful of aggregates (counts,
sums, distributions, per-key counters, position buckets). `fetch_aggregate` reads those aggregates
from `telemetry_events` (`base.telemetry.metrics.aggregate_sql`, a few statements in one
connection, nothing materialized per row) and returns an `EventAggregate`;
`build_report_from_aggregate` / `agent_rollups_from_aggregate` rebuild the report from it, reusing
the helper functions (`pctiles`, `third_of`, `fix_kinds`) so the math cannot drift.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict

import psycopg

from base.telemetry.metrics import (
    AgentActivityData,
    ExecData,
    LlmTurnsData,
    MetricSection,
    PluginActivationData,
    PositionBlocks,
    SyntaxFixData,
    fix_kinds,
    pctiles,
    render_agent_activity,
    render_exec,
    render_llm_turns,
    render_plugin_activation,
    render_syntax_fix,
    third_of,
)
from base.telemetry.metrics.aggregate_sql import (
    AgentCounters,
    LlmCostSums,
    read_agent_window,
    read_window,
)


# meiva:note
# The `meta` header of the report: the window, its filters and the event totals.
class ReportMeta(TypedDict):
    window_days: int
    agent_filter: int | None
    generated_at: str
    total_events: int
    distinct_agents: int
    since_compact: bool


# meiva:note
# The `metrics` map of the report: one fragment per section, keyed by the section name.
class ReportMetrics(TypedDict):
    syntax_fix: SyntaxFixData
    exec: ExecData
    llm_turns: LlmTurnsData
    agent_activity: AgentActivityData
    plugin_activation: PluginActivationData


# meiva:note
# The machine report `build_report_from_aggregate` returns and `/api/metrics` serves.
class ReportData(TypedDict):
    meta: ReportMeta
    metrics: ReportMetrics


# meiva:note
# One agent's headline counters for `/api/metrics/agents`. `cache_hit_pct` is None when the agent
# has input tokens but no call reported cache metering.
class AgentRollup(TypedDict):
    events: int
    cost_usd: float
    llm_calls: int
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    cache_hit_pct: float | None
    turn_ok: int
    turn_total: int
    exec_ok: int
    exec_failed: int


@dataclass(frozen=True)
class _LlmTotals:
    """Token + cost totals over one set of llm_usage rows — call count, token
    totals, cost, unpriced count (the SQL aggregate's per-model sums)."""

    calls: int
    tin: int
    tout: int
    tcached: int
    treason: int
    cost: float
    unpriced: int


@dataclass
class _PerAgentAgg:
    """Per-agent row aggregates — turn counts, exec outcomes, event span.

    Includes the service-level group (agent_id None): the units count it toward
    `turns_per_agent` and `agent_lifetime_s`, so the aggregate must too; the /agents
    endpoint filters it.
    """

    agent_id: int | None
    events: int
    turn_total: int
    turn_ok: int
    exec_ok: int
    exec_failed: int
    first_ts: datetime
    last_ts: datetime


@dataclass
class EventAggregate:
    """The window reduced in SQL — compact input for the report assemblers.

    Distributions that only feed `pctiles` keep their values (ints/floats); per-key counters are
    `{key: count}` mappings in descending count order.
    """

    total_events: int
    distinct_agents: int
    # syntax_fix
    code_blocks: int
    code_blocks_per_agent: dict[int | None, int]
    fix_events: list[tuple[int | None, int, str]]  # (agent_id, block_no, fixes) — time order
    # exec
    exec_ok: int
    exec_failed: int
    failure_types: dict[str, int]  # exc_type (else event name) -> count
    code_len: list[float]
    output_len: list[float]
    # llm / turns
    llm_by_model: list[
        tuple[str, int, int, int, int, int]
    ]  # (model, calls, in, out, cached, reasoning)
    llm_position: dict[str, tuple[int, int]]  # third -> (cache_read, in_total)
    llm_costs: dict[int | None, dict[str, LlmCostSums]]
    llm_cache_input: dict[int | None, int]
    turn_total: int
    turn_ok: int
    turn_durations: list[float]
    per_agent: dict[int | None, _PerAgentAgg]
    per_agent_llm: dict[
        int | None, list[tuple[str, int, int, int, int]]
    ]  # (model, calls, in, out, cached)
    # agent activity
    spawners: dict[str, int]  # spawner -> count
    lifecycle: dict[str, int]
    idle_halts: int
    # plugin activation
    plugin_acts: dict[tuple[str, str, str, str], int]  # (plugin, surface, identifier, model)


def _per_agent_aggs(
    per_agent: dict[int | None, AgentCounters], fallback: datetime
) -> dict[int | None, _PerAgentAgg]:
    return {
        agent: _PerAgentAgg(
            agent,
            row.events,
            row.turn_total,
            row.turn_ok,
            row.exec_ok,
            row.exec_failed,
            row.first_ts or fallback,
            row.last_ts or fallback,
        )
        for agent, row in per_agent.items()
        if row.events
    }


def _llm_by_agent(
    llm_per_agent: dict[int | None, dict[str, tuple[int, int, int, int, int]]],
) -> dict[int | None, list[tuple[str, int, int, int, int]]]:
    return {
        agent: [(model, n, i, o, c) for model, (n, i, o, c, _r) in models.items()]
        for agent, models in llm_per_agent.items()
    }


def _llm_by_model(
    llm_per_agent: dict[int | None, dict[str, tuple[int, int, int, int, int]]],
) -> list[tuple[str, int, int, int, int, int]]:
    """Per-model `(model, calls, in, out, cached, reasoning)` summed over the agents."""
    by_model: dict[str, list[int]] = {}
    for models in llm_per_agent.values():
        for model, sums in models.items():
            acc = by_model.setdefault(model, [0, 0, 0, 0, 0])
            for slot, value in enumerate(sums):
                acc[slot] += value
    return [(m, acc[0], acc[1], acc[2], acc[3], acc[4]) for m, acc in by_model.items()]


def fetch_aggregate(
    conn: psycopg.Connection,
    days: int,
    agent_id: int | None,
    *,
    now: datetime,
    since_compact: bool = False,
) -> EventAggregate:
    """Windowed aggregate over `telemetry_events` — the gateway + CLI entry point.

    Category telemetry|log over [now-days, now], optional single-agent scope, optional
    since_compact (each agent's rows narrowed to at-or-after its latest compact halt; service rows
    always kept).
    """
    data = read_window(conn, days, agent_id, now=now, since_compact=since_compact)
    raw = data["per_agent"]
    rows = list(raw.values())
    lengths = data["lengths"]
    return EventAggregate(
        total_events=sum(r.events for r in rows),
        distinct_agents=len([a for a in raw if a is not None]),
        code_blocks=sum(r.code for r in rows),
        code_blocks_per_agent={a: r.code for a, r in raw.items() if r.code},
        fix_events=data["fix_events"],
        exec_ok=sum(r.exec_ok for r in rows),
        exec_failed=sum(r.exec_failed for r in rows),
        failure_types=data["failure_types"],
        code_len=lengths["code_len"],
        output_len=lengths["output_len"],
        llm_by_model=_llm_by_model(data["llm_per_agent"]),
        llm_position=data["llm_position"],
        llm_costs=data["llm_costs"],
        llm_cache_input=data["llm_cache_input"],
        turn_total=sum(r.turn_total for r in rows),
        turn_ok=sum(r.turn_ok for r in rows),
        turn_durations=lengths["turn_dur"],
        per_agent=_per_agent_aggs(raw, data["start"]),
        per_agent_llm=_llm_by_agent(data["llm_per_agent"]),
        spawners=data["spawners"],
        lifecycle={
            "spawned": sum(r.spawned for r in rows),
            "resurrected": sum(r.resurrected for r in rows),
        },
        idle_halts=sum(r.idle_halts for r in rows),
        plugin_acts=data["plugin_acts"],
    )


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
    per_agent = _per_agent_aggs(raw, now)
    total = sum(r.events for r in raw.values())
    return total, agent_rollups(
        per_agent, _llm_by_agent(data["llm_per_agent"]), data["llm_costs"], data["llm_cache_input"]
    )


def _cost_totals(rows: Iterable[tuple[str, LlmCostSums]]) -> tuple[float, int]:
    """Sum the recorded cost snapshots and the explicitly unpriced call counts."""
    cost = 0.0
    unpriced = 0
    for _model, row in rows:
        cost += row.cost
        unpriced += row.unpriced
    return cost, unpriced


def _llm_totals_from_models(
    rows: list[tuple[str, int, int, int, int, int]],
    costs: dict[int | None, dict[str, LlmCostSums]],
) -> _LlmTotals:
    """Sum token counts and recorded USD independently."""
    calls = sum(r[1] for r in rows)
    tin = sum(r[2] for r in rows)
    tout = sum(r[3] for r in rows)
    tcached = sum(r[4] for r in rows)
    treason = sum(r[5] for r in rows)
    cost, unpriced = _cost_totals(row for models in costs.values() for row in models.items())
    return _LlmTotals(calls, tin, tout, tcached, treason, cost, unpriced)


def _data_syntax_fix(agg: EventAggregate) -> SyntaxFixData:
    """The `syntax_fix` section data."""
    # syntax_fix: per-event kinds (Counter, stream order) + per-block
    # ruff_format position thirds (block index i among the agent's n blocks ->
    # third_of(i, n)).
    kind_counts: Counter[str] = Counter()
    hits: dict[str, int] = {"early": 0, "mid": 0, "late": 0}
    blocks_b: dict[str, int] = {"early": 0, "mid": 0, "late": 0}
    for _agent_id, blk, fixes in agg.fix_events:
        if blk <= 0:
            continue
        kinds = set(fix_kinds(fixes))
        kind_counts.update(kinds)
        b = third_of(blk - 1, agg.code_blocks_per_agent.get(_agent_id, 0))
        if "ruff_format" in kinds:
            hits[b] += 1
    for _agent_id, n in agg.code_blocks_per_agent.items():
        for i in range(n):
            blocks_b[third_of(i, n)] += 1
    rate = {
        b: round(hits[b] / blocks_b[b] * 100, 1) if blocks_b[b] else 0.0
        for b in ("early", "mid", "late")
    }
    return {
        "code_blocks": agg.code_blocks,
        "trigger_counts": dict(kind_counts),
        "ruff_format_by_position": {
            b: PositionBlocks(hits=hits[b], blocks=blocks_b[b]) for b in ("early", "mid", "late")
        },
        "ruff_format_rate_pct_by_position": rate,
    }


def _data_exec(agg: EventAggregate) -> ExecData:
    """The `exec` section data."""
    total_exec = agg.exec_ok + agg.exec_failed
    return {
        "exec_total": total_exec,
        "exec_ok": agg.exec_ok,
        "exec_failed": agg.exec_failed,
        "success_rate_pct": round(agg.exec_ok / total_exec * 100, 1) if total_exec else 0.0,
        "failure_types": dict(agg.failure_types),
        "code_len_chars": pctiles(agg.code_len),
        "output_len_chars": pctiles(agg.output_len),
    }


def _data_llm_turns(agg: EventAggregate) -> LlmTurnsData:
    """The `llm_turns` section data."""
    totals = _llm_totals_from_models(agg.llm_by_model, agg.llm_costs)
    cache_input = sum(agg.llm_cache_input.values())
    # position thirds over each agent's llm_usage rows: (cache_read, in_total) sums.
    pos_hit = {
        b: round(c / i * 100, 1) if i else (None if totals.tin else 0.0)
        for b, (c, i) in ((b, agg.llm_position.get(b, (0, 0))) for b in ("early", "mid", "late"))
    }
    turns_per_agent = [row.turn_total for row in agg.per_agent.values()]
    return {
        "llm_calls": totals.calls,
        "tokens_in": totals.tin,
        "tokens_out": totals.tout,
        "tokens_cached": totals.tcached,
        "tokens_reasoning": totals.treason,
        "cache_hit_pct": round(totals.tcached / cache_input * 100, 1)
        if cache_input
        else (None if totals.tin else 0.0),
        "cache_hit_pct_by_position": pos_hit,
        "cost_usd": round(totals.cost, 4),
        "cost_unpriced_calls": totals.unpriced,
        "turn_duration_s": pctiles(agg.turn_durations),
        "turn_ok": agg.turn_ok,
        "turn_total": agg.turn_total,
        "turns_per_agent": pctiles([float(x) for x in turns_per_agent]),
    }


def _data_agent_activity(agg: EventAggregate) -> AgentActivityData:
    """The `agent_activity` section data."""
    by_spawner: Counter[str] = Counter()
    subagents = 0
    for sp, count in agg.spawners.items():
        by_spawner[sp.split(":", 1)[0]] += count
        if sp.startswith("agent:"):
            subagents += count
    spans = [
        (row.last_ts - row.first_ts).total_seconds()
        for row in agg.per_agent.values()
        if row.events >= 2
    ]
    return {
        "distinct_agents": agg.distinct_agents,
        "spawns_total": sum(agg.spawners.values()),
        "spawns_by_spawner": dict(by_spawner),
        "subagent_spawns": subagents,
        "lifecycle": {k: agg.lifecycle.get(k, 0) for k in ("spawned", "resurrected")},
        "idle_halts": agg.idle_halts,
        "agent_lifetime_s": pctiles(spans),
    }


def _data_plugin_activation(agg: EventAggregate) -> PluginActivationData:
    """The `plugin_activation` section data."""
    # plugin_activation — philosophy §6's "removable as a gauge, not a vibe".
    # `by_contribution` is keyed the way `meiva plugins inspect` spells a
    # registered contribution, so a row with no counterpart here is a
    # contribution that registered and never fired; `by_plugin_model` is the
    # per-model cut that answers "does model X still need this shim".
    act_plugin: Counter[str] = Counter()
    act_contribution: Counter[str] = Counter()
    act_plugin_model: Counter[str] = Counter()
    for (plugin, surface, identifier, model), count in agg.plugin_acts.items():
        act_plugin[plugin] += count
        act_contribution[f"{plugin}/{surface}/{identifier}"] += count
        act_plugin_model[f"{plugin}@{model or '?'}"] += count
    return {
        "total_activations": sum(agg.plugin_acts.values()),
        "distinct_plugins": len(act_plugin),
        "by_plugin": dict(act_plugin),
        "by_contribution": [
            {"contribution": c, "count": n} for c, n in act_contribution.most_common()
        ],
        "by_plugin_model": dict(act_plugin_model),
    }


# meiva:note
# Rebuild the five unit data fragments from `EventAggregate`.
def _metrics_from_aggregate(agg: EventAggregate) -> ReportMetrics:
    return {
        "syntax_fix": _data_syntax_fix(agg),
        "exec": _data_exec(agg),
        "llm_turns": _data_llm_turns(agg),
        "agent_activity": _data_agent_activity(agg),
        "plugin_activation": _data_plugin_activation(agg),
    }


# meiva:note
# Render the five fragments with the shared render functions — the text blocks stay identical to
# the per-row path because the renders are the same functions.
def _sections_from_metrics(metrics: ReportMetrics) -> list[MetricSection]:
    return [
        MetricSection(
            "syntax_fix", render_syntax_fix(metrics["syntax_fix"]), metrics["syntax_fix"]
        ),
        MetricSection("exec", render_exec(metrics["exec"]), metrics["exec"]),
        MetricSection("llm_turns", render_llm_turns(metrics["llm_turns"]), metrics["llm_turns"]),
        MetricSection(
            "agent_activity",
            render_agent_activity(metrics["agent_activity"]),
            metrics["agent_activity"],
        ),
        MetricSection(
            "plugin_activation",
            render_plugin_activation(metrics["plugin_activation"]),
            metrics["plugin_activation"],
        ),
    ]


def build_report_from_aggregate(
    agg: EventAggregate,
    days: int,
    agent_id: int | None,
    *,
    now: datetime,
    since_compact: bool = False,
) -> tuple[str, ReportData]:
    """Assemble the same text + data `build_report` produces, from an
    `EventAggregate` (see `fetch_aggregate`). Meta semantics are identical:
    `total_events` counts every telemetry/log row in the window (service-level
    rows included), `distinct_agents` excludes agent_id NULL."""
    metrics = _metrics_from_aggregate(agg)
    sections = _sections_from_metrics(metrics)
    names = [s.name for s in sections]
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate metric section names would clobber the JSON: {names}")
    generated_at = now.isoformat()
    meta: ReportMeta = {
        "window_days": days,
        "agent_filter": agent_id,
        "generated_at": generated_at,
        "total_events": agg.total_events,
        "distinct_agents": agg.distinct_agents,
        "since_compact": since_compact,
    }
    header = (
        "=" * 64
        + f"\n  Meiva metrics — last {days}d"
        + (" — since last compact" if since_compact else "")
        + (f" — agent {agent_id}" if agent_id is not None else "")
        + f"\n  {agg.total_events} events / {agg.distinct_agents} agents"
        + f" / generated {generated_at[:19]}\n"
        + "=" * 64
        + "\n"
    )
    text = header + "\n" + "\n".join(s.text for s in sections)
    return text, {"meta": meta, "metrics": metrics}


def agent_rollups(
    per_agent: dict[int | None, _PerAgentAgg],
    per_agent_llm: dict[int | None, list[tuple[str, int, int, int, int]]],
    costs: dict[int | None, dict[str, LlmCostSums]],
    cache_inputs: dict[int | None, int],
) -> dict[int, AgentRollup]:
    """Per-agent headline counters for `/api/metrics/agents` (service rows excluded)."""
    out: dict[int, AgentRollup] = {}
    for aid, row in per_agent.items():
        if aid is None:
            continue
        usage = per_agent_llm.get(aid, [])
        calls = sum(r[1] for r in usage)
        tin = sum(r[2] for r in usage)
        tout = sum(r[3] for r in usage)
        tcached = sum(r[4] for r in usage)
        cache_input = cache_inputs.get(aid, 0)
        cost, _unpriced = _cost_totals(costs.get(aid, {}).items())
        out[aid] = {
            "events": row.events,
            "cost_usd": round(cost, 4),
            "llm_calls": calls,
            "tokens_in": tin,
            "tokens_out": tout,
            "tokens_cached": tcached,
            "cache_hit_pct": round(tcached / cache_input * 100, 1)
            if cache_input
            else (None if tin else 0.0),
            "turn_ok": row.turn_ok,
            "turn_total": row.turn_total,
            "exec_ok": row.exec_ok,
            "exec_failed": row.exec_failed,
        }
    return out
