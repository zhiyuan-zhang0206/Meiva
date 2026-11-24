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


def _rollups(stream: TelemetryStream, *, days: int = 1) -> dict[int, AgentRollup]:
    return fetch_agent_rollups(stream.db, days, now=stream.now)


def _usage(
    fake: TelemetryStream,
    agent_id: int | None,
    *,
    model: str = "claude-opus-5-5",
    billing: str = "api",
    complete: bool = True,
    ts_offset_days: float = 0,
    **fields: Any,
) -> None:
    cost = fields.get("cost_usd")
    fake.db.execute(
        """INSERT INTO llm_usage_ledger
           (call_id, occurred_at, agent_id, machine, cluster, billing, usage_kind, model,
            calls, complete, tokens_in, tokens_out, tokens_reasoning, cache_read,
            cost_usd, price_snapshot, price_source)
           VALUES (gen_random_uuid()::text, %s, %s, 'm', 'c', %s, 'agent', %s,
                   1, %s, %s, %s, %s, %s, %s, '{}'::jsonb, %s)""",
        (
            fake.now - timedelta(days=ts_offset_days, microseconds=1),
            agent_id,
            billing,
            model,
            complete,
            fields.get("tokens_in"),
            fields.get("tokens_out"),
            fields.get("tokens_reasoning"),
            fields.get("cache_read"),
            cost,
            "live" if cost is not None else None,
        ),
    )


def test_an_empty_window_has_no_rollups(fake: TelemetryStream) -> None:
    assert _rollups(fake) == {}


def test_recorded_zero_is_priced_and_a_missing_price_is_counted_not_invented(
    fake: TelemetryStream,
) -> None:
    _usage(fake, 1, cost_usd=0.00614, tokens_in=1000, tokens_out=50, cache_read=200)
    _usage(fake, 1, cost_usd=0, tokens_in=1000, tokens_out=50, cache_read=200)
    _usage(fake, 1, tokens_in=1000, tokens_out=50, cache_read=200)
    _usage(fake, None, cost_usd=0.01228, tokens_in=1000, tokens_out=50, cache_read=200)
    rollups = _rollups(fake)
    assert set(rollups) == {1}
    assert rollups[1]["cost_usd"] == pytest.approx(0.0061)
    assert rollups[1]["usage_unpriced_calls"] == 1
    assert rollups[1]["llm_calls"] == 3
    assert rollups[1]["tokens_in"] == 3000


def test_the_subscription_slice_is_split_from_the_api_cost(fake: TelemetryStream) -> None:
    _usage(fake, 1, cost_usd=2, tokens_in=10)
    _usage(fake, 1, cost_usd=3, tokens_in=10, billing="subscription")
    _usage(fake, 1, cost_usd=1, tokens_in=10, complete=False)
    row = _rollups(fake)[1]
    assert (row["cost_usd"], row["cost_usd_api"], row["cost_usd_subscription"]) == (6, 3, 3)
    assert row["usage_incomplete_reports"] == 1


def test_agents_are_rolled_up_separately_over_their_own_window(fake: TelemetryStream) -> None:
    _usage(fake, 1, tokens_in=10, tokens_out=1)
    _usage(fake, 2, tokens_in=20, tokens_out=2)
    _usage(fake, 1, tokens_in=500, tokens_out=50, ts_offset_days=5)
    one_day = _rollups(fake, days=1)
    assert (one_day[1]["tokens_in"], one_day[2]["tokens_in"]) == (10, 20)
    seven_day = _rollups(fake, days=7)
    assert (seven_day[1]["tokens_in"], seven_day[1]["llm_calls"]) == (510, 2)


@pytest.mark.parametrize("cached", [None, 0, 800])
def test_cache_rates_exclude_unmetered_usage_without_losing_cost_or_tokens(
    fake: TelemetryStream, cached: int | None
) -> None:
    _usage(fake, 1, tokens_in=1000, cost_usd=1, cache_read=cached)
    _usage(fake, 1, tokens_in=5000, tokens_out=0, cost_usd=2)
    row = _rollups(fake)[1]
    expected = None if cached is None else cached / 10
    assert row["llm_calls"] == 2
    assert row["cache_hit_pct"] == expected
    assert row["tokens_in"] == 6000
    assert row["cost_usd"] == 3
