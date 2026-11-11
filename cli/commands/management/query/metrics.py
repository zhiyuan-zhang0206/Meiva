# meiva:note
# `meiva metrics agents` and `meiva stats` — the fleet's cost, token and alert figures read
# through `GET /api/metrics/agents` and `GET /api/stats/dashboard`.
#
# Thin clients: the gateway aggregates in SQL (per-agent cost and turn outcomes over the last N
# days; fleet totals, alert counts and the plugin status cards over a stats window), and these
# verbs print what it returns. A window omitted here is the gateway's configured default.

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from base.cluster.machine import GatewayAccess
from cli.boundary.gateway_read import fetch, print_json, render_table


class _AgentMetrics(BaseModel):
    agent_id: int
    label: str | None
    events: int
    cost_usd: float
    llm_calls: int
    tokens_in: int
    tokens_out: int
    cache_hit_pct: float | None
    turn_ok: int
    turn_total: int
    exec_ok: int
    exec_failed: int


class _MetricsMeta(BaseModel):
    window_days: int
    total_events: int
    distinct_agents: int


class _AgentMetricsReport(BaseModel):
    meta: _MetricsMeta
    agents: list[_AgentMetrics]


# meiva:note
# Print one row per agent for the window, the costliest first (the gateway's order).
def cmd_metrics_agents(
    access: GatewayAccess, *, days: int | None, since_compact: bool, as_json: bool
) -> int:
    resp = fetch(
        access,
        "/api/metrics/agents",
        {"days": days, "since_compact": True if since_compact else None},
    )
    if resp is None:
        return 1
    if as_json:
        print_json(resp)
        return 0
    report = _AgentMetricsReport.model_validate_json(resp.content)
    if not report.agents:
        print(f"(no agent activity in the last {report.meta.window_days} day(s))")
        return 0
    print(
        render_table(
            (
                "agent",
                "cost_usd",
                "calls",
                "tokens_in",
                "tokens_out",
                "cache%",
                "turns",
                "exec_ok",
                "exec_failed",
                "events",
                "label",
            ),
            [
                (
                    str(row.agent_id),
                    f"{row.cost_usd:.2f}",
                    str(row.llm_calls),
                    str(row.tokens_in),
                    str(row.tokens_out),
                    "" if row.cache_hit_pct is None else f"{row.cache_hit_pct:.1f}",
                    f"{row.turn_ok}/{row.turn_total}",
                    str(row.exec_ok),
                    str(row.exec_failed),
                    str(row.events),
                    row.label or "",
                )
                for row in report.agents
            ],
        )
    )
    print(
        f"({len(report.agents)} agents, {report.meta.total_events} events, "
        f"last {report.meta.window_days} day(s))"
    )
    return 0


class _Tokens(BaseModel):
    input: int
    output: int
    cache_read: int
    cache_hit_pct: float | None


class _PluginStat(BaseModel):
    plugin: str
    id: str
    value: str
    detail: str | None
    status: str
    updated_at: datetime


class _StatsDashboard(BaseModel):
    live_count: int
    window_hours: int
    tokens: _Tokens
    cost_usd: float
    subscription_cost_usd: float
    openai_cost_usd: float | None
    anthropic_cost_usd: float | None
    warnings: int
    errors: int
    alert_classes_active: int
    alert_classes_dismissed: int
    plugin_stats: list[_PluginStat]


# meiva:note
# The money of a window: a provider slice the gateway could not resolve prints as "unknown".
def _usd(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.2f}"


# meiva:note
# Print the fleet totals of one stats window, then the plugin status cards (balances and quotas).
def cmd_stats(access: GatewayAccess, *, hours: int | None, as_json: bool) -> int:
    resp = fetch(access, "/api/stats/dashboard", {"hours": hours})
    if resp is None:
        return 1
    if as_json:
        print_json(resp)
        return 0
    stats = _StatsDashboard.model_validate_json(resp.content)
    window = "last 5 minutes" if stats.window_hours == 0 else f"last {stats.window_hours} hour(s)"
    hit = (
        ""
        if stats.tokens.cache_hit_pct is None
        else f", cache hit {stats.tokens.cache_hit_pct:.1f}%"
    )
    print(f"window:        {window}")
    print(f"live agents:   {stats.live_count}")
    print(
        f"tokens:        in {stats.tokens.input}, out {stats.tokens.output}, "
        f"cache read {stats.tokens.cache_read}{hit}"
    )
    print(
        f"cost (usd):    api {stats.cost_usd:.2f} (openai {_usd(stats.openai_cost_usd)}, "
        f"anthropic {_usd(stats.anthropic_cost_usd)}), subscription {stats.subscription_cost_usd:.2f}"
    )
    print(
        f"alerts:        {stats.warnings} warnings, {stats.errors} errors; "
        f"{stats.alert_classes_active} classes active, {stats.alert_classes_dismissed} resolved"
    )
    if stats.plugin_stats:
        print()
        print(
            render_table(
                ("plugin", "id", "status", "value", "updated", "detail"),
                [
                    (
                        stat.plugin,
                        stat.id,
                        stat.status,
                        stat.value,
                        stat.updated_at.isoformat(timespec="seconds"),
                        stat.detail or "",
                    )
                    for stat in stats.plugin_stats
                ],
            )
        )
    return 0
