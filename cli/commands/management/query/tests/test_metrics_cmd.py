# meiva:note
# `meiva metrics agents` / `meiva stats` against a gateway on a loopback port: the command dials it
# through its real transport and headers, and each case asserts the request the gateway saw (a
# window not named is not sent) and what the operator was shown.

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import JsonValue

from cli.commands.cluster.tests.gateway_server import Reply, access_to, serving
from cli.commands.management.query.metrics import cmd_metrics_agents, cmd_stats

AGENTS = ("GET", "/api/metrics/agents")
DASHBOARD = ("GET", "/api/stats/dashboard")


def _agent(agent_id: int, *, cost: float, label: str | None = None) -> dict[str, JsonValue]:
    return {
        "agent_id": agent_id,
        "label": label,
        "events": 120,
        "cost_usd": cost,
        "llm_calls": 40,
        "tokens_in": 1000,
        "tokens_out": 200,
        "tokens_cached": 500,
        "cache_hit_pct": 50.0,
        "turn_ok": 9,
        "turn_total": 10,
        "exec_ok": 30,
        "exec_failed": 2,
    }


def _report(agents: list[dict[str, JsonValue]]) -> dict[str, object]:
    return {
        "meta": {
            "window_days": 1,
            "agent_filter": None,
            "generated_at": "2026-11-12T01:00:00+00:00",
            "total_events": 500,
            "distinct_agents": len(agents),
            "since_compact": False,
        },
        "agents": agents,
    }


def _query(target: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(target).query)


def test_metrics_agents_without_a_window_sends_none_and_prints_the_table(
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = _report([_agent(7, cost=3.456, label="ops"), _agent(8, cost=0.5)])
    with serving({AGENTS: Reply(body=body)}) as gateway:
        code = cmd_metrics_agents(
            access_to(gateway.url), days=None, since_compact=False, as_json=False
        )
    assert code == 0
    assert [r.target for r in gateway.requests] == ["/api/metrics/agents"]
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == [
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
    ]
    assert out[1].split() == [
        "7",
        "3.46",
        "40",
        "1000",
        "200",
        "50.0",
        "9/10",
        "30",
        "2",
        "120",
        "ops",
    ]
    assert out[2].split()[0:2] == ["8", "0.50"]
    assert out[3] == "(2 agents, 500 events, last 1 day(s))"


def test_metrics_agents_forwards_days_and_since_compact() -> None:
    with serving({AGENTS: Reply(body=_report([]))}) as gateway:
        cmd_metrics_agents(access_to(gateway.url), days=7, since_compact=True, as_json=False)
    assert _query(gateway.requests[0].target) == {"days": ["7"], "since_compact": ["true"]}


def test_metrics_agents_says_when_nothing_ran(capsys: pytest.CaptureFixture[str]) -> None:
    with serving({AGENTS: Reply(body=_report([]))}) as gateway:
        assert (
            cmd_metrics_agents(
                access_to(gateway.url), days=None, since_compact=False, as_json=False
            )
            == 0
        )
    assert capsys.readouterr().out == "(no agent activity in the last 1 day(s))\n"


def test_metrics_agents_json_is_the_response_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    body = _report([_agent(7, cost=1.0)])
    with serving({AGENTS: Reply(body=body)}) as gateway:
        cmd_metrics_agents(access_to(gateway.url), days=None, since_compact=False, as_json=True)
    assert json.loads(capsys.readouterr().out) == body


def test_a_refused_window_prints_the_gateways_reason_and_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    refusal = Reply(
        status=422, body={"detail": [{"msg": "Input should be less than or equal to 30"}]}
    )
    with serving({AGENTS: refusal}) as gateway:
        assert (
            cmd_metrics_agents(access_to(gateway.url), days=99, since_compact=False, as_json=False)
            == 1
        )
    assert capsys.readouterr().err.startswith("gateway 422: ")


def _dashboard(plugin_stats: list[dict[str, JsonValue]]) -> dict[str, object]:
    return {
        "live_count": 12,
        "window_hours": 24,
        "applied_window_hours": 24,
        "tokens": {"input": 1000, "output": 200, "cache_read": 500, "cache_hit_pct": 50.0},
        "cost_usd": 12.5,
        "subscription_cost_usd": 3.0,
        "openai_cost_usd": None,
        "anthropic_cost_usd": 4.25,
        "warnings": 3,
        "errors": 1,
        "warnings_resolved": 0,
        "errors_resolved": 0,
        "alert_classes_active": 2,
        "alert_classes_dismissed": 1,
        "plugin_stats": plugin_stats,
        "as_of": "2026-11-12T01:00:00+00:00",
    }


def test_stats_prints_the_totals_and_the_plugin_cards(capsys: pytest.CaptureFixture[str]) -> None:
    cards: list[dict[str, JsonValue]] = [
        {
            "plugin": "meiva_usage",
            "id": "deepseek-balance",
            "value": "CNY 42.10",
            "detail": None,
            "status": "ok",
            "updated_at": "2026-11-12T00:55:00+00:00",
            "updated_by": None,
        }
    ]
    with serving({DASHBOARD: Reply(body=_dashboard(cards))}) as gateway:
        assert cmd_stats(access_to(gateway.url), hours=None, as_json=False) == 0
    assert [r.target for r in gateway.requests] == ["/api/stats/dashboard"]
    out = capsys.readouterr().out.splitlines()
    assert out[:6] == [
        "window:        last 24 hour(s)",
        "live agents:   12",
        "tokens:        in 1000, out 200, cache read 500, cache hit 50.0%",
        "cost (usd):    api 12.50 (openai unknown, anthropic 4.25), subscription 3.00",
        "alerts:        3 warnings, 1 errors; 2 classes active, 1 resolved",
        "",
    ]
    assert out[6].split() == ["plugin", "id", "status", "value", "updated", "detail"]
    assert out[7].split() == [
        "meiva_usage",
        "deepseek-balance",
        "ok",
        "CNY",
        "42.10",
        "2026-11-12T00:55:00+00:00",
    ]


def test_stats_forwards_hours_and_names_the_five_minute_window(
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = _dashboard([])
    body["window_hours"] = 0
    with serving({DASHBOARD: Reply(body=body)}) as gateway:
        cmd_stats(access_to(gateway.url), hours=0, as_json=False)
    assert _query(gateway.requests[0].target) == {"hours": ["0"]}
    assert capsys.readouterr().out.startswith("window:        last 5 minutes\n")


def test_stats_json_is_the_response_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    body = _dashboard([])
    with serving({DASHBOARD: Reply(body=body)}) as gateway:
        cmd_stats(access_to(gateway.url), hours=None, as_json=True)
    assert json.loads(capsys.readouterr().out) == body
