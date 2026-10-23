from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import psycopg

from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client
from gateway.tests.events.event_rows import telemetry_event

_UNITS = {
    "syntax_fix",
    "exec",
    "llm_turns",
    "agent_activity",
    "plugin_activation",
}


@contextmanager
def _metrics_client(*, default_window_days: int | None = None) -> Generator[GatewayTestClient]:
    fields = (
        {} if default_window_days is None else {"metrics_default_window_days": default_window_days}
    )
    with gateway_test_client(fields=fields) as client:
        yield client


def _insert_agent(db: psycopg.Connection, *, spawner: str = "user") -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES (%s) RETURNING id", (f"agent-{spawner}",))
        row = cur.fetchone()
    assert row is not None, "INSERT ... RETURNING always yields a row"
    return row[0]


def _insert_event(
    db: psycopg.Connection,
    *,
    event: str,
    agent_id: int | None = None,
    payload: dict[str, object] | None = None,
    ts_offset_days: float = 0,
) -> None:
    telemetry_event(
        db,
        event,
        minutes_ago=ts_offset_days * 24 * 60,
        category="log" if event == "log" else "telemetry",
        agent_id=agent_id,
        attributes=payload or {},
    )


def test_metrics_empty_db_returns_envelope_with_all_units(db_conn: psycopg.Connection) -> None:
    db_conn.commit()
    with _metrics_client() as client:
        resp = client.get("/api/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["total_events"] == 0
    assert body["meta"]["distinct_agents"] == 0
    assert body["meta"]["window_days"] == 1
    assert body["meta"]["agent_filter"] is None
    assert set(body["metrics"]) == _UNITS


def test_metrics_wires_payload_field_names(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn)
    _insert_event(db_conn, event="code", agent_id=aid, payload={"body": "print(1)"})
    _insert_event(db_conn, event="syntax_fix", agent_id=aid, payload={"fixes": "ruff,ruff_format"})
    _insert_event(db_conn, event="exec", agent_id=aid, payload={"body": "1\n", "ok": True})
    _insert_event(
        db_conn,
        event="llm_usage",
        agent_id=aid,
        payload={"in_total": 1000, "out_total": 200, "cache_read": 800, "model": "deepseek-v4-pro"},
    )
    _insert_event(db_conn, event="agent_spawned", agent_id=aid, payload={"spawner": "agent:1"})
    _insert_event(db_conn, event="halt", agent_id=aid, payload={"body": "no tool_call (idle)"})
    _insert_event(db_conn, event="halt", agent_id=aid, payload={"body": "system_halt (compact)"})
    db_conn.commit()
    with _metrics_client() as client:
        body = client.get("/api/metrics").json()
    m = body["metrics"]
    assert m["syntax_fix"]["trigger_counts"] == {"ruff": 1, "ruff_format": 1}
    assert m["exec"]["exec_ok"] == 1
    assert m["exec"]["exec_total"] == 1
    assert m["llm_turns"]["tokens_in"] == 1000
    assert m["llm_turns"]["tokens_cached"] == 800
    assert m["llm_turns"]["cache_hit_pct"] == 80.0
    assert m["agent_activity"]["spawns_by_spawner"] == {"agent": 1}
    assert m["agent_activity"]["subagent_spawns"] == 1
    assert m["agent_activity"]["idle_halts"] == 1
    assert body["meta"]["distinct_agents"] == 1


def test_metrics_agent_filter_scopes_to_one(db_conn: psycopg.Connection) -> None:
    a1 = _insert_agent(db_conn, spawner="one")
    a2 = _insert_agent(db_conn, spawner="two")
    _insert_event(db_conn, event="code", agent_id=a1, payload={"body": "x"})
    _insert_event(db_conn, event="code", agent_id=a2, payload={"body": "y"})
    db_conn.commit()
    with _metrics_client() as client:
        body = client.get(f"/api/metrics?agent={a1}").json()
    assert body["meta"]["agent_filter"] == a1
    assert body["meta"]["distinct_agents"] == 1
    assert body["metrics"]["exec"]["code_len_chars"]["n"] == 1


def test_metrics_null_agent_events_not_counted(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn)
    _insert_event(db_conn, event="code", agent_id=aid, payload={"body": "x"})
    _insert_event(db_conn, event="log", agent_id=None, payload={})
    db_conn.commit()
    with _metrics_client() as client:
        body = client.get("/api/metrics").json()
    assert body["meta"]["total_events"] == 2
    assert body["meta"]["distinct_agents"] == 1


def test_metrics_window_filters_old_rows(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn)
    _insert_event(db_conn, event="code", agent_id=aid, payload={"body": "old"}, ts_offset_days=3)
    _insert_event(db_conn, event="code", agent_id=aid, payload={"body": "new"}, ts_offset_days=0)
    db_conn.commit()
    with _metrics_client() as client:
        one_day = client.get("/api/metrics?days=1").json()
        seven_day = client.get("/api/metrics?days=7").json()
    assert one_day["meta"]["total_events"] == 1
    assert seven_day["meta"]["total_events"] == 2


def test_metrics_days_out_of_range_rejected(db_conn: psycopg.Connection) -> None:
    db_conn.commit()
    with gateway_test_client() as client:
        assert client.get("/api/metrics?days=0").status_code == 422
        assert client.get("/api/metrics?days=31").status_code == 422


def test_metrics_default_window_comes_from_display_config(db_conn: psycopg.Connection) -> None:
    db_conn.commit()
    with _metrics_client(default_window_days=3) as client:
        resp = client.get("/api/metrics")
        agents_resp = client.get("/api/metrics/agents")
    assert resp.status_code == 200
    assert resp.json()["meta"]["window_days"] == 3
    assert agents_resp.status_code == 200
    assert agents_resp.json()["meta"]["window_days"] == 3


def test_metrics_since_compact_param(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn)
    _insert_event(
        db_conn,
        event="llm_usage",
        agent_id=aid,
        payload={"in_total": 1000, "out_total": 100, "cache_read": 0, "model": "deepseek-v4-pro"},
        ts_offset_days=0.5,
    )
    _insert_event(
        db_conn,
        event="halt",
        agent_id=aid,
        payload={"body": "system_halt (compact)"},
        ts_offset_days=0.3,
    )
    _insert_event(
        db_conn,
        event="llm_usage",
        agent_id=aid,
        payload={"in_total": 200, "out_total": 20, "cache_read": 0, "model": "deepseek-v4-pro"},
        ts_offset_days=0.1,
    )
    db_conn.commit()
    with _metrics_client() as client:
        full = client.get("/api/metrics").json()
        since = client.get("/api/metrics?since_compact=true").json()
    assert full["meta"]["since_compact"] is False
    assert full["metrics"]["llm_turns"]["llm_calls"] == 2
    assert full["metrics"]["llm_turns"]["tokens_in"] == 1200
    assert since["meta"]["since_compact"] is True
    assert since["metrics"]["llm_turns"]["llm_calls"] == 1
    assert since["metrics"]["llm_turns"]["tokens_in"] == 200
    assert since["meta"]["total_events"] == 2


def test_metrics_agents_empty(db_conn: psycopg.Connection) -> None:
    db_conn.commit()
    with _metrics_client() as client:
        body = client.get("/api/metrics/agents?days=1").json()
    assert body["agents"] == []
    assert body["meta"]["total_events"] == 0
    assert body["meta"]["distinct_agents"] == 0
    assert body["meta"]["window_days"] == 1
    assert body["meta"]["since_compact"] is False


def _assert_expensive_agent_metrics(row: dict[str, Any]) -> None:
    assert row["label"] == "agent-two"
    assert row["cost_usd"] == 30.0
    assert row["llm_calls"] == 1
    assert row["exec_ok"] == 1
    assert row["exec_failed"] == 0


def _assert_cheap_agent_metrics(row: dict[str, Any]) -> None:
    assert row["label"] == "agent-one"
    assert row["cost_usd"] == 0
    assert row["events"] == 3
    assert row["tokens_in"] == 1000
    assert row["cache_hit_pct"] == 80.0
    assert row["turn_ok"] == 1
    assert row["turn_total"] == 1
    assert row["exec_failed"] == 1


def test_metrics_agents_multi_agent(db_conn: psycopg.Connection) -> None:
    a1 = _insert_agent(db_conn, spawner="one")
    a2 = _insert_agent(db_conn, spawner="two")
    _insert_event(
        db_conn,
        event="llm_usage",
        agent_id=a1,
        payload={
            "in_total": 1000,
            "out_total": 100,
            "cache_read": 800,
            "model": "no-such-model",
            "unpriced": 1,
        },
    )
    _insert_event(
        db_conn, event="turn_end", agent_id=a1, payload={"duration_seconds": 1, "ok": True}
    )
    _insert_event(db_conn, event="exec_failed", agent_id=a1, payload={"exc_type": "ValueError"})
    _insert_event(
        db_conn,
        event="llm_usage",
        agent_id=a2,
        payload={
            "in_total": 1_000_000,
            "out_total": 1_000_000,
            "cache_read": 0,
            "model": "claude-opus-4-8",
            "cost_usd": 30.0,
        },
    )
    _insert_event(db_conn, event="exec", agent_id=a2, payload={"body": "ok"})
    _insert_event(db_conn, event="log", agent_id=None, payload={})
    db_conn.commit()
    with _metrics_client() as client:
        body = client.get("/api/metrics/agents?days=1").json()
    assert body["meta"]["total_events"] == 6
    assert body["meta"]["distinct_agents"] == 2
    assert [a["agent_id"] for a in body["agents"]] == [a2, a1]
    expensive, cheap = body["agents"]
    _assert_expensive_agent_metrics(expensive)
    _assert_cheap_agent_metrics(cheap)


def test_metrics_agents_since_compact(db_conn: psycopg.Connection) -> None:
    compacted = _insert_agent(db_conn, spawner="compacted")
    plain = _insert_agent(db_conn, spawner="plain")
    _insert_event(
        db_conn,
        event="llm_usage",
        agent_id=compacted,
        payload={"in_total": 1000, "out_total": 100, "cache_read": 0, "model": "deepseek-v4-pro"},
        ts_offset_days=0.5,
    )
    _insert_event(
        db_conn,
        event="halt",
        agent_id=compacted,
        payload={"body": "system_halt (compact)"},
        ts_offset_days=0.3,
    )
    _insert_event(
        db_conn,
        event="llm_usage",
        agent_id=compacted,
        payload={"in_total": 200, "out_total": 20, "cache_read": 0, "model": "deepseek-v4-pro"},
        ts_offset_days=0.1,
    )
    _insert_event(
        db_conn,
        event="llm_usage",
        agent_id=plain,
        payload={"in_total": 500, "out_total": 50, "cache_read": 0, "model": "deepseek-v4-pro"},
        ts_offset_days=0.5,
    )
    db_conn.commit()
    with _metrics_client() as client:
        full = client.get("/api/metrics/agents?days=1").json()
        since = client.get("/api/metrics/agents?days=1&since_compact=true").json()
    by_id = {a["agent_id"]: a for a in full["agents"]}
    assert by_id[compacted]["llm_calls"] == 2
    assert by_id[plain]["llm_calls"] == 1
    assert since["meta"]["since_compact"] is True
    by_id = {a["agent_id"]: a for a in since["agents"]}
    assert by_id[compacted]["llm_calls"] == 1
    assert by_id[compacted]["tokens_in"] == 200
    assert by_id[compacted]["events"] == 2
    assert by_id[plain]["llm_calls"] == 1
