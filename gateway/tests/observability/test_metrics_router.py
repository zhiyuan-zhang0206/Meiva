from __future__ import annotations

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import psycopg

from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client


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


def _insert_usage(
    db: psycopg.Connection,
    *,
    agent_id: int | None = None,
    payload: dict[str, object],
    ts_offset_days: float = 0,
) -> None:
    source = payload.get("source")
    db.execute(
        """
        INSERT INTO llm_usage_ledger
            (call_id, occurred_at, agent_id, machine, cluster, source, billing, usage_kind,
             model, calls, complete, tokens_in, tokens_out, cache_read, tokens_reasoning,
             cost_usd, price_source, price_snapshot)
        VALUES (%s, now() - (%s * interval '1 day'), %s, 'm1', 'c', %s,
                CASE WHEN %s IN ('claude_code','codex','antigravity') THEN 'subscription'
                     ELSE 'api' END,
                'test', %s, %s, %s, %s, %s, %s, %s, %s, %s, '{}')
        """,
        (
            str(uuid.uuid4()),
            ts_offset_days,
            agent_id,
            source,
            source,
            payload.get("model", ""),
            payload.get("calls", 1),
            payload.get("complete", True),
            payload.get("in_total"),
            payload.get("out_total"),
            payload.get("cache_read"),
            payload.get("reasoning"),
            payload.get("cost_usd"),
            "live" if payload.get("cost_usd") is not None else None,
        ),
    )


def test_metrics_agents_makes_no_row_for_service_usage(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn)
    usage: dict[str, object] = {"in_total": 10, "out_total": 1, "model": "deepseek-v4-pro"}
    _insert_usage(db_conn, agent_id=aid, payload=usage)
    _insert_usage(db_conn, agent_id=None, payload=usage)
    db_conn.commit()
    with _metrics_client() as client:
        body = client.get("/api/metrics/agents").json()
    assert body["meta"]["distinct_agents"] == 1
    assert [row["agent_id"] for row in body["agents"]] == [aid]


def test_metrics_agents_window_filters_old_rows(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn)
    usage: dict[str, object] = {"in_total": 10, "out_total": 1, "model": "deepseek-v4-pro"}
    _insert_usage(db_conn, agent_id=aid, payload=usage, ts_offset_days=3)
    _insert_usage(db_conn, agent_id=aid, payload=usage, ts_offset_days=0)
    db_conn.commit()
    with _metrics_client() as client:
        one_day = client.get("/api/metrics/agents?days=1").json()
        seven_day = client.get("/api/metrics/agents?days=7").json()
    assert one_day["agents"][0]["llm_calls"] == 1
    assert seven_day["agents"][0]["llm_calls"] == 2


def test_metrics_agents_days_out_of_range_rejected(db_conn: psycopg.Connection) -> None:
    db_conn.commit()
    with gateway_test_client() as client:
        assert client.get("/api/metrics/agents?days=0").status_code == 422
        assert client.get("/api/metrics/agents?days=31").status_code == 422


def test_metrics_agents_default_window_comes_from_display_config(
    db_conn: psycopg.Connection,
) -> None:
    db_conn.commit()
    with _metrics_client(default_window_days=3) as client:
        resp = client.get("/api/metrics/agents")
    assert resp.status_code == 200
    assert resp.json()["meta"]["window_days"] == 3


def test_metrics_agents_empty(db_conn: psycopg.Connection) -> None:
    db_conn.commit()
    with _metrics_client() as client:
        body = client.get("/api/metrics/agents?days=1").json()
    assert body["agents"] == []
    assert body["meta"]["distinct_agents"] == 0
    assert body["meta"]["window_days"] == 1


def _assert_expensive_agent_metrics(row: dict[str, Any]) -> None:
    assert row["label"] == "agent-two"
    assert row["cost_usd"] == 30.0
    assert row["cost_usd_api"] == 0
    assert row["cost_usd_subscription"] == 30.0
    assert row["usage_incomplete_reports"] == 1
    assert row["llm_calls"] == 1


def _assert_cheap_agent_metrics(row: dict[str, Any]) -> None:
    assert row["label"] == "agent-one"
    assert row["cost_usd"] == 0
    assert row["tokens_in"] == 1000
    assert row["cache_hit_pct"] == 80.0


def test_metrics_agents_multi_agent(db_conn: psycopg.Connection) -> None:
    a1 = _insert_agent(db_conn, spawner="one")
    a2 = _insert_agent(db_conn, spawner="two")
    _insert_usage(
        db_conn,
        agent_id=a1,
        payload={
            "in_total": 1000,
            "out_total": 100,
            "cache_read": 800,
            "model": "no-such-model",
            "unpriced": 1,
        },
    )
    _insert_usage(
        db_conn,
        agent_id=a2,
        payload={
            "in_total": 1_000_000,
            "out_total": 1_000_000,
            "cache_read": 0,
            "model": "claude-opus-4-8",
            "source": "claude_code",
            "complete": False,
            "cost_usd": 30.0,
        },
    )
    db_conn.commit()
    with _metrics_client() as client:
        body = client.get("/api/metrics/agents?days=1").json()
    assert body["meta"]["distinct_agents"] == 2
    assert [a["agent_id"] for a in body["agents"]] == [a2, a1]
    expensive, cheap = body["agents"]
    _assert_expensive_agent_metrics(expensive)
    _assert_cheap_agent_metrics(cheap)
