from __future__ import annotations

from datetime import timedelta
from typing import Any

import psycopg
import pytest

from base.telemetry.metrics.aggregate import AgentRollup, fetch_agent_rollups
from base.telemetry.metrics.tests.telemetry_stream import TelemetryStream


@pytest.fixture
def fake(db_conn: psycopg.Connection) -> TelemetryStream:
    return TelemetryStream(db_conn)


def _rollups(
    stream: TelemetryStream, *, days: int = 1, since_compact: bool = False
) -> tuple[int, dict[int, AgentRollup]]:
    return fetch_agent_rollups(stream.db, days, now=stream.now, since_compact=since_compact)


def _add(
    fake: TelemetryStream,
    *,
    event: str,
    agent_id: int | None,
    payload: dict[str, Any] | None = None,
    ts_offset_days: float = 0,
) -> None:
    attrs = payload or {}
    fake.add(event=event, agent_id=agent_id, payload=attrs, ts_offset_hours=ts_offset_days * 24)
    if event == "llm_usage":
        _add_ledger_usage(fake, agent_id, attrs, ts_offset_days=ts_offset_days)


def _add_ledger_usage(
    fake: TelemetryStream,
    agent_id: int | None,
    attrs: dict[str, Any],
    *,
    ts_offset_days: float,
) -> None:
    cost = attrs.get("cost_usd")
    fake.db.execute(
        """INSERT INTO llm_usage_ledger
           (call_id, occurred_at, agent_id, machine, cluster, billing, usage_kind, model,
            calls, complete, tokens_in, tokens_out, tokens_reasoning, cache_read,
            cache_write_5m, cache_write_1h, cost_usd, price_snapshot, price_source)
           VALUES (gen_random_uuid()::text, %s, %s, 'm', 'c', 'api', 'agent', %s,
                   1, true, %s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, %s)""",
        (
            fake.now - timedelta(days=ts_offset_days, microseconds=1),
            agent_id,
            attrs["model"],
            attrs.get("in_total"),
            attrs.get("out_total"),
            attrs.get("reasoning"),
            attrs.get("cache_read"),
            attrs.get("cache_write_5m"),
            attrs.get("cache_write_1h"),
            cost,
            "live" if cost is not None else None,
        ),
    )


def test_an_empty_window_has_no_rollups(fake: TelemetryStream) -> None:
    assert _rollups(fake) == (0, {})
    assert _rollups(fake, since_compact=True) == (0, {})


def test_cache_write_cost_snapshots_survive_aggregation_and_unpriced_calls(
    fake: TelemetryStream,
) -> None:
    base = {
        "model": "claude-opus-5-5",
        "in_total": 1000,
        "out_total": 50,
        "cache_read": 200,
        "cache_write_5m": 300,
        "cache_write_1h": 400,
    }
    _add(fake, event="llm_usage", agent_id=1, payload={**base, "cost_usd": 0.00614})
    _add(fake, event="llm_usage", agent_id=1, payload={**base, "cost_usd": 0})
    _add(fake, event="llm_usage", agent_id=1, payload={**base, "unpriced": 1})
    _add(fake, event="llm_usage", agent_id=None, payload={**base, "cost_usd": 0.01228})
    _total, rollups = _rollups(fake)
    assert set(rollups) == {1}
    assert rollups[1]["cost_usd"] == pytest.approx(round(0.00614, 4))
    assert rollups[1]["usage_unpriced_calls"] == 1
    assert rollups[1]["llm_calls"] == 3


def test_a_full_thread_rolls_up_from_the_ledger_and_the_event_rows(fake: TelemetryStream) -> None:
    aid = 1
    _add(fake, event="code", agent_id=aid, payload={"body": "print(1)"})
    _add(fake, event="exec", agent_id=aid, payload={"body": "1\n", "ok": True})
    usage = {
        "in_total": 1000,
        "out_total": 200,
        "cache_read": 800,
        "model": "mimo-v2.5-pro",
        "cost_usd": 0.0003,
    }
    fake.add(event="llm_usage", agent_id=aid, payload={**usage, "in_total": 9999, "cost_usd": 99})
    _add_ledger_usage(fake, aid, usage, ts_offset_days=0)
    _add(fake, event="turn_end", agent_id=aid, payload={"ok": True, "duration_seconds": 4.0})
    _add(fake, event="agent_spawned", agent_id=aid, payload={"spawner": "agent:1"})
    _add(fake, event="halt", agent_id=aid, payload={"body": "no tool_call (idle)"})

    total, roll = _rollups(fake)
    assert total == 6
    row = roll[aid]
    assert row["events"] == 6
    assert (row["llm_calls"], row["tokens_in"], row["tokens_out"]) == (1, 1000, 200)
    assert row["cache_hit_pct"] == 80.0
    assert row["cost_usd"] == pytest.approx(0.0003)
    assert (row["turn_total"], row["turn_ok"]) == (1, 1)
    assert (row["exec_ok"], row["exec_failed"]) == (1, 0)

    total2, roll2 = _rollups(fake, since_compact=True)
    assert total2 == 6
    assert roll2[aid]["events"] == 6


def test_service_rows_count_toward_the_total_but_make_no_rollup(fake: TelemetryStream) -> None:
    a1, a2 = 1, 2
    for aid, body, off in ((a1, "x=1", 0), (a2, "y=2", 0), (a1, "z=3", 0.5)):
        _add(fake, event="code", agent_id=aid, payload={"body": body}, ts_offset_days=off)
    _add(fake, event="log", agent_id=None, payload={}, ts_offset_days=0.1)
    _add(fake, event="log", agent_id=None, payload={}, ts_offset_days=0.9)
    _add(fake, event="turn_end", agent_id=a1, payload={"ok": True, "duration_seconds": 1.0})
    _add(fake, event="turn_end", agent_id=a2, payload={"ok": False, "duration_seconds": 2.0})
    total, roll = _rollups(fake)
    assert total == 7
    assert set(roll) == {a1, a2}
    assert (roll[a1]["turn_ok"], roll[a1]["turn_total"]) == (1, 1)
    assert (roll[a2]["turn_ok"], roll[a2]["turn_total"]) == (0, 1)


def test_every_exec_failure_variant_counts_as_failed(fake: TelemetryStream) -> None:
    aid = 1
    _add(fake, event="exec", agent_id=aid, payload={"body": "ok"})
    _add(fake, event="exec_failed", agent_id=aid, payload={"body": "t", "exc_type": "ValueError"})
    _add(fake, event="exec_failed", agent_id=aid, payload={"body": "t"})
    _add(fake, event="exec_timeout", agent_id=aid, payload={"body": "t"})
    _add(fake, event="exec_cancelled", agent_id=aid, payload={"body": "t"})
    _total, roll = _rollups(fake)
    assert (roll[aid]["exec_ok"], roll[aid]["exec_failed"]) == (1, 4)


def test_since_compact_drops_rows_before_each_agents_latest_compact(
    fake: TelemetryStream,
) -> None:
    a1, a2 = 1, 2
    _add(fake, event="code", agent_id=a1, payload={"body": "old"}, ts_offset_days=0.8)
    _add(
        fake,
        event="halt",
        agent_id=a1,
        payload={"body": "system_halt (compact)"},
        ts_offset_days=0.6,
    )
    _add(fake, event="code", agent_id=a1, payload={"body": "new"}, ts_offset_days=0.2)
    _add(
        fake,
        event="halt",
        agent_id=a1,
        payload={"body": "system_halt (compact)"},
        ts_offset_days=0.1,
    )
    _add(fake, event="code", agent_id=a1, payload={"body": "post"}, ts_offset_days=0.05)
    _add(fake, event="code", agent_id=a2, payload={"body": "other"}, ts_offset_days=0.7)
    _add(fake, event="log", agent_id=None, payload={}, ts_offset_days=0.9)
    total, roll = _rollups(fake, since_compact=True)
    assert total == 4
    assert roll[a1]["events"] == 2
    assert roll[a2]["events"] == 1
    total2, roll2 = _rollups(fake)
    assert total2 == 7
    assert roll2[a1]["events"] == 5
    assert roll2[a2]["events"] == 1


def test_the_window_bounds_the_rows(fake: TelemetryStream) -> None:
    aid = 1
    _add(fake, event="code", agent_id=aid, payload={"body": "old"}, ts_offset_days=5)
    _add(fake, event="code", agent_id=aid, payload={"body": "new"})
    assert _rollups(fake, days=1)[1][aid]["events"] == 1
    assert _rollups(fake, days=7)[1][aid]["events"] == 2


@pytest.mark.parametrize("cached", [None, 0, 800])
def test_cache_rates_exclude_unmetered_usage_without_losing_cost_or_tokens(
    fake: TelemetryStream, cached: int | None
) -> None:
    _add(
        fake,
        event="llm_usage",
        agent_id=1,
        payload={
            "model": "chat",
            "in_total": 1000,
            "cost_usd": 1,
            **({"cache_read": cached} if cached is not None else {}),
        },
    )
    _add(
        fake,
        event="llm_usage",
        agent_id=1,
        payload={"model": "decision", "in_total": 5000, "out_total": 0, "cost_usd": 2},
    )
    _total, rollups = _rollups(fake)
    expected = None if cached is None else cached / 10
    assert rollups[1]["llm_calls"] == 2
    assert rollups[1]["cache_hit_pct"] == expected
    assert rollups[1]["tokens_in"] == 6000
    assert rollups[1]["cost_usd"] == 3
