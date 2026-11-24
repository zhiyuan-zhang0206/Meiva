from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from base.telemetry.metrics import usage

Row = tuple[int, str, int, int, int, int, int, float, int]


def _row(
    agent_id: int,
    model: str,
    tin: int,
    tout: int,
    cached: int,
    reason: int,
    calls: int,
    cost: float,
    unpriced: int,
) -> Row:
    return (agent_id, model, tin, tout, cached, reason, calls, cost, unpriced)


def test_single_group_folds_cost_snapshot() -> None:
    rows = [_row(1464, "claude-fable-5", 1_000_000, 500_000, 200_000, 10_000, 7, 1.2345, 0)]
    out = usage.aggregate(rows)
    agent = out["per_agent"]["1464"]
    assert agent["cost_usd"] == 1.2345
    assert agent["llm_calls"] == 7
    assert agent["tokens_reasoning"] == 10_000
    assert agent["unpriced_calls"] == 0
    assert out["total"]["cost_usd"] == 1.2345
    assert out["total"]["distinct_agents"] == 1


def test_unpriced_group_zero_cost_but_counted() -> None:
    rows = [_row(1, "no-such-model-x", 5_000, 5_000, 0, 0, 3, 0.0, 3)]
    out = usage.aggregate(rows)
    agent = out["per_agent"]["1"]
    assert agent["cost_usd"] == 0.0
    assert agent["unpriced_calls"] == 3
    assert agent["llm_calls"] == 3
    assert agent["by_model"]["no-such-model-x"]["cost_usd"] is None


def test_per_agent_costs_sum_to_total() -> None:
    rows = [
        _row(1, "claude-fable-5", 800_000, 400_000, 100_000, 0, 4, 0.5, 0),
        _row(1, "claude-haiku-4-5-20251001", 2_000_000, 300_000, 500_000, 0, 9, 0.25, 0),
        _row(2, "claude-sonnet-5", 1_200_000, 600_000, 300_000, 5_000, 6, 0.75, 1),
    ]
    out = usage.aggregate(rows)
    a1 = out["per_agent"]["1"]["cost_usd"]
    a2 = out["per_agent"]["2"]["cost_usd"]
    assert out["total"]["distinct_agents"] == 2
    assert out["total"]["cost_usd"] == a1 + a2
    assert a1 == 0.75
    assert out["per_agent"]["1"]["llm_calls"] == 13
    assert out["per_agent"]["2"]["unpriced_calls"] == 1


def _agent(db: psycopg.Connection) -> int:
    row = db.execute("INSERT INTO agents (label) VALUES ('usage') RETURNING id").fetchone()
    assert row is not None
    db.commit()
    return int(row[0])


def _report(
    db: psycopg.Connection,
    agent_id: int | None,
    occurred_at: datetime,
    *,
    model: str = "m",
    cost: str | None = None,
    calls: int = 1,
    billing: str = "api",
    tokens_in: int | None = 100,
    tokens_out: int | None = 50,
    cache_read: int | None = 10,
    reasoning: int | None = 5,
) -> None:
    db.execute(
        "INSERT INTO llm_usage_ledger (call_id, occurred_at, agent_id, machine, cluster, billing, "
        "usage_kind, model, calls, complete, tokens_in, tokens_out, tokens_reasoning, cache_read, "
        "cost_usd, price_snapshot, price_source) VALUES (%s, %s, %s, 'm', 'c', %s, 'turn', %s, %s, "
        "true, %s, %s, %s, %s, %s, %s, %s)",
        (
            f"test:{uuid.uuid4()}",
            occurred_at,
            agent_id,
            billing,
            model,
            calls,
            tokens_in,
            tokens_out,
            reasoning,
            cache_read,
            Decimal(cost) if cost is not None else None,
            Jsonb({}),
            "live" if cost is not None else None,
        ),
    )
    db.commit()


_START = datetime(2026, 11, 1, tzinfo=UTC)
_END = _START + timedelta(days=1)


def _sums(db: psycopg.Connection, agents: list[int], start: datetime | None, end: datetime):
    return usage.aggregate(usage.ledger_rows(db, agents, start, end))["per_agent"]


def test_a_window_sums_the_ledger_reports_per_agent_and_model(db_conn: psycopg.Connection) -> None:
    a, b = _agent(db_conn), _agent(db_conn)
    at = _START + timedelta(hours=1)
    _report(db_conn, a, at, cost="0.5")
    _report(db_conn, a, at, cost="0.25", billing="subscription")
    _report(db_conn, a, at)
    _report(db_conn, a, at, model="other", cost="1.0", tokens_in=1)
    _report(db_conn, a, _START - timedelta(seconds=1), cost="9.0")
    _report(db_conn, a, _END, cost="9.0")
    _report(db_conn, b, at, cost="2.0")
    _report(db_conn, None, at, cost="3.0")

    out = _sums(db_conn, [a], _START, _END)

    agent = out[str(a)]
    assert agent["llm_calls"] == 4
    assert agent["tokens_in"] == 301
    assert agent["tokens_cached"] == 40
    assert agent["tokens_reasoning"] == 20
    assert agent["unpriced_calls"] == 1
    assert agent["cost_usd"] == 1.75
    assert agent["by_model"]["m"]["llm_calls"] == 3
    assert str(b) not in out
    assert set(_sums(db_conn, [a, b], _START, _END)) == {str(a), str(b)}


def test_a_report_counts_every_call_it_describes_and_unpriced_calls_are_calls(
    db_conn: psycopg.Connection,
) -> None:
    agent = _agent(db_conn)
    at = _START + timedelta(hours=1)
    _report(db_conn, agent, at, calls=3, cost="0.3")
    _report(db_conn, agent, at, calls=4)
    _report(
        db_conn,
        agent,
        at,
        calls=2,
        tokens_in=None,
        tokens_out=None,
        cache_read=None,
        reasoning=None,
    )

    out = _sums(db_conn, [agent], _START, _END)[str(agent)]

    assert (out["llm_calls"], out["unpriced_calls"]) == (9, 6)
    assert (out["tokens_in"], out["tokens_out"]) == (200, 100)
    assert out["cost_usd"] == 0.3


def test_the_beginning_of_the_ledger_is_the_start_of_a_lifetime_and_any_end_is_exact(
    db_conn: psycopg.Connection,
) -> None:
    agent = _agent(db_conn)
    for age_days, cost in ((400, "3.0"), (40, "2.0"), (3, "4.0")):
        _report(db_conn, agent, _END - timedelta(days=age_days), cost=cost, tokens_in=age_days)

    everything = _sums(db_conn, [agent], None, _END)[str(agent)]
    older = _sums(db_conn, [agent], None, _END - timedelta(days=10))[str(agent)]

    assert (everything["llm_calls"], everything["tokens_in"], everything["cost_usd"]) == (
        3,
        443,
        9.0,
    )
    assert (older["llm_calls"], older["tokens_in"], older["cost_usd"]) == (2, 440, 5.0)
    assert _sums(db_conn, [agent], _END, _END + timedelta(days=1)) == {}


def test_repeated_model_groups_preserve_cost_precision_and_totals() -> None:
    report = usage.aggregate(
        [(1, "m", 10, 5, 2, 1, 1, 0.000001, 0), (1, "m", 20, 3, 4, 1, 1, 0.000002, 0)]
    )
    agent = report["per_agent"]["1"]
    assert agent["by_model"]["m"]["tokens_in"] == 30
    assert agent["by_model"]["m"]["llm_calls"] == 2
    assert agent["by_model"]["m"]["cost_usd"] == agent["cost_usd"]
    assert report["total"]["cost_usd"] == 0.000001 + 0.000002
