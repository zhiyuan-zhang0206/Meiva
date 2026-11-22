from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from typing import Any

import psycopg
import pytest

from base.lm.pricing import (
    CacheTokens,
    ModelPrice,
    PriceBook,
    PricePeriod,
    PriceRates,
    PriceTier,
    plugin_model_price,
    quote,
)
from base.telemetry.delivery.mirror import event_id as mirror_event_id
from base.telemetry.event_store import event_uid
from base.telemetry.metrics import observed_metrics
from services.upkeep.events_maintenance import rollup, token_totals, unpriced_backfill

_OLD_DAY = date(2026, 11, 15)
_RECENT_DAY = date(2026, 11, 21)
_TODAY = date(2026, 11, 27)


@pytest.fixture
def db(db_conn: psycopg.Connection) -> psycopg.Connection:
    db_conn.autocommit = True
    return db_conn


def _agent(db: psycopg.Connection, label: str = "backfill-agent") -> int:
    row = db.execute("INSERT INTO agents (label) VALUES (%s) RETURNING id", (label,)).fetchone()
    assert row is not None
    return int(row[0])


def _insert_telemetry_and_obs(
    db: psycopg.Connection,
    *,
    agent_id: int,
    occurred_at: datetime,
    model: str,
    tokens_in: int,
    tokens_out: int,
    tokens_cached: int = 0,
    cache_write_5m: int = 0,
    cost_usd: float | None = None,
    source: str | None = "antigravity",
    tag: str,
) -> int:
    attrs: dict[str, Any] = {
        "model": model,
        "in_total": tokens_in,
        "out_total": tokens_out,
        "reasoning": 0,
    }
    if tokens_cached:
        attrs["cache_read"] = tokens_cached
    if cache_write_5m:
        attrs["cache_write_5m"] = cache_write_5m
    if source is not None:
        attrs["source"] = source
    if cost_usd is not None:
        attrs["cost_usd"] = cost_usd
    else:
        attrs["unpriced"] = 1

    ts_ns = int(occurred_at.timestamp() * 1_000_000_000)
    stream_id = mirror_event_id(f"{tag}:{agent_id}:{model}:{tokens_in}:{tokens_out}", ts_ns)
    db.execute(
        "INSERT INTO telemetry_events (event_uid, ts, agent_id, machine, cluster, process, "
        "category, event_name, level, source, attributes) VALUES (%s, %s, %s, 'm', 'c', 'p', "
        "'telemetry', 'llm_usage', 'info', 'test', %s::jsonb)",
        (event_uid(stream_id), occurred_at, agent_id, json.dumps(attrs)),
    )
    obs = observed_metrics.observe_row(
        {
            "id": stream_id,
            "agent_id": agent_id,
            "ts": occurred_at,
            "category": "telemetry",
            "event_name": "llm_usage",
            "attributes": attrs,
        }
    )
    assert obs is not None
    assert observed_metrics.write_observations([obs], db=db) == 1
    return stream_id


def _versioned_price_book() -> PriceBook:
    neon: ModelPrice = plugin_model_price(
        "gemini-4-neon",
        PriceRates(
            cache_miss=2.0,
            cache_hit=0.2,
            output=12.0,
            vendor="google",
            source_url="https://example.com/pricing",
            source_checked_at="2026-11-22",
            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until="2026-11-20T00:00:00Z",
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="1.0",
                            cache_hit="0.1",
                            output="4.0",
                        ),
                    ),
                ),
                PricePeriod(
                    effective_from="2026-11-20T00:00:00Z",
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=None,
                            cache_miss="2.0",
                            cache_hit="0.2",
                            output="12.0",
                        ),
                    ),
                ),
            ),
        ),
    )
    fable: ModelPrice = plugin_model_price(
        "claude-fable-5-2",
        PriceRates(
            cache_miss=5.0,
            cache_hit=0.5,
            output=25.0,
            cache_write_5m=6.25,
            vendor="anthropic",
            source_url="https://example.com/pricing",
            source_checked_at="2026-11-22",
        ),
    )
    return PriceBook({"gemini-4-neon": neon, "claude-fable-5-2": fable}, {})


def _seed_two_period_scenario(db: psycopg.Connection, agent: int) -> None:
    old_ts = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
    _insert_telemetry_and_obs(
        db,
        agent_id=agent,
        occurred_at=old_ts,
        model="gemini-4-neon-high",
        tokens_in=1_000_000,
        tokens_out=500_000,
        cost_usd=None,
        source="antigravity",
        tag="old-unpriced",
    )
    _insert_telemetry_and_obs(
        db,
        agent_id=agent,
        occurred_at=old_ts + timedelta(minutes=5),
        model="gemini-4-neon",
        tokens_in=1_000_000,
        tokens_out=500_000,
        cost_usd=0.7777,
        source="antigravity",
        tag="old-already-priced",
    )
    recent_ts = datetime(2026, 11, 21, 14, 0, tzinfo=UTC)
    _insert_telemetry_and_obs(
        db,
        agent_id=agent,
        occurred_at=recent_ts,
        model="gemini-4-neon-medium",
        tokens_in=1_000_000,
        tokens_out=500_000,
        tokens_cached=500_000,
        cost_usd=None,
        source="antigravity",
        tag="recent-unpriced",
    )
    _insert_telemetry_and_obs(
        db,
        agent_id=agent,
        occurred_at=recent_ts + timedelta(minutes=10),
        model="claude-fable-5-2[1m]",
        tokens_in=1_000_000,
        tokens_out=0,
        cache_write_5m=200_000,
        cost_usd=None,
        source=None,
        tag="recent-claude-api",
    )
    rollup.roll_day(db, _OLD_DAY)
    rollup.roll_day(db, _RECENT_DAY)
    token_totals.fold_totals(db, today=_TODAY)


def _assert_backfilled_tables(db: psycopg.Connection, agent: int) -> None:
    after_days = db.execute(
        "SELECT COALESCE(sum(unpriced_calls), 0), COALESCE(sum(unpriced_calls_subscription), 0), "
        "COALESCE(sum(cost_usd), 0), COALESCE(sum(cost_usd_subscription), 0) "
        "FROM agent_metric_days WHERE agent_id = %s",
        (agent,),
    ).fetchone()
    assert after_days == (0, 0, Decimal("16.1277"), Decimal("10.8777"))

    obs_costs = db.execute(
        "SELECT model, cost_usd, unpriced_calls FROM agent_metric_observations "
        "WHERE agent_id = %s ORDER BY occurred_at",
        (agent,),
    ).fetchall()
    assert obs_costs == [
        ("gemini-4-neon", Decimal("3.0"), 0),
        ("gemini-4-neon", Decimal("0.7777"), 0),
        ("gemini-4-neon", Decimal("7.1"), 0),
        ("claude-fable-5-2", Decimal("5.25"), 0),
    ]

    daily_rows = db.execute(
        "SELECT day, model, llm_calls, costed_calls, unpriced_calls, cost_usd, "
        "cost_usd_subscription FROM agent_model_tokens_daily WHERE agent_id = %s "
        "ORDER BY day, model",
        (agent,),
    ).fetchall()
    assert daily_rows == [
        (_OLD_DAY, "gemini-4-neon", 2, 2, 0, pytest.approx(3.7777), pytest.approx(3.7777)),
        (_RECENT_DAY, "claude-fable-5-2", 1, 1, 0, pytest.approx(5.25), pytest.approx(0.0)),
        (_RECENT_DAY, "gemini-4-neon", 1, 1, 0, pytest.approx(7.1), pytest.approx(7.1)),
    ]

    total_rows = db.execute(
        "SELECT model, llm_calls, costed_calls, unpriced_calls, cost_usd "
        "FROM agent_model_tokens_total WHERE agent_id = %s ORDER BY model",
        (agent,),
    ).fetchall()
    assert total_rows == [("gemini-4-neon", 2, 2, 0, pytest.approx(3.7777))]


def test_backfill_prices_null_rows_at_occurred_at_preserves_existing_prices_and_is_idempotent(
    db: psycopg.Connection,
) -> None:
    agent = _agent(db)
    book = _versioned_price_book()
    _seed_two_period_scenario(db, agent)
    assert token_totals.folded_through(db) == date(2026, 11, 17)

    dry = unpriced_backfill.run_unpriced_backfill(db, today=_TODAY, prices=book, dry_run=True)
    assert (dry.dry_run, dry.observations_scanned, dry.observations_priced) == (True, 3, 3)
    assert (dry.metric_days_recomputed, dry.ledger_days_updated, dry.ledger_calls_priced) == (
        2,
        2,
        3,
    )
    assert (dry.added_observation_cost_usd, dry.added_observation_cost_usd_subscription) == (
        Decimal("15.35"),
        Decimal("10.1"),
    )
    assert db.execute(
        "SELECT count(*) FROM agent_metric_observations WHERE agent_id = %s AND unpriced_calls > 0",
        (agent,),
    ).fetchone() == (3,)

    applied = unpriced_backfill.run_unpriced_backfill(db, today=_TODAY, prices=book, dry_run=False)
    assert (applied.observations_priced, applied.observations_still_unpriced) == (3, 0)
    assert (
        applied.metric_days_recomputed,
        applied.ledger_days_updated,
        applied.totals_rebuilt,
    ) == (2, 2, True)
    _assert_backfilled_tables(db, agent)

    rollup.roll_day(db, _RECENT_DAY, prices=book)
    assert db.execute(
        "SELECT day, model, llm_calls, unpriced_calls, cost_usd "
        "FROM agent_model_tokens_daily WHERE agent_id = %s AND day = %s ORDER BY model",
        (agent, _RECENT_DAY),
    ).fetchall() == [
        (_RECENT_DAY, "claude-fable-5-2", 1, 0, pytest.approx(5.25)),
        (_RECENT_DAY, "gemini-4-neon", 1, 0, pytest.approx(7.1)),
    ]

    rerun = unpriced_backfill.run_unpriced_backfill(db, today=_TODAY, prices=book, dry_run=False)
    assert (
        rerun.observations_scanned,
        rerun.observations_priced,
        rerun.ledger_days_scanned,
        rerun.totals_rebuilt,
    ) == (0, 0, 0, False)


def test_backfill_leaves_still_unpriced_models_untouched_while_pricing_cataloged_ones(
    db: psycopg.Connection,
) -> None:
    agent = _agent(db, "partial-catalog")
    book = _versioned_price_book()
    ts = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)

    _insert_telemetry_and_obs(
        db,
        agent_id=agent,
        occurred_at=ts,
        model="gemini-4-neon-xhigh",
        tokens_in=500_000,
        tokens_out=0,
        cost_usd=None,
        source="antigravity",
        tag="known-unpriced",
    )
    _insert_telemetry_and_obs(
        db,
        agent_id=agent,
        occurred_at=ts + timedelta(seconds=1),
        model="brand-new-unpriced-high",
        tokens_in=500_000,
        tokens_out=0,
        cost_usd=None,
        source="antigravity",
        tag="unknown-unpriced",
    )

    report = unpriced_backfill.run_unpriced_backfill(db, today=_TODAY, prices=book, dry_run=False)
    assert report.observations_scanned == 2
    assert report.observations_priced == 1
    assert report.observations_still_unpriced == 1
    assert report.added_observation_cost_usd == Decimal("1.0")

    day_row = db.execute(
        "SELECT cost_usd, unpriced_calls, unpriced_calls_subscription "
        "FROM agent_metric_days WHERE agent_id = %s AND day = %s",
        (agent, _RECENT_DAY),
    ).fetchone()
    assert day_row == (Decimal("1.0"), 1, 1)


def test_archive_price_book_covers_historical_antigravity_and_claude_models() -> None:
    book = unpriced_backfill.load_price_book()
    historical_samples = (
        ("gemini-4-neon-medium", datetime(2026, 11, 21, 10, 17, tzinfo=UTC), "gemini-4-neon"),
        ("gemini-4-neon-high", datetime(2026, 11, 21, 18, 0, tzinfo=UTC), "gemini-4-neon"),
        ("gemini-4-neon-xhigh", datetime(2026, 11, 22, 6, 0, tzinfo=UTC), "gemini-4-neon"),
        ("gemini-4-carbon-high", datetime(2026, 11, 14, 8, 14, tzinfo=UTC), "gemini-4-carbon"),
        ("claude-fable-5-2", datetime(2026, 11, 11, 0, 0, tzinfo=UTC), "claude-fable-5-2"),
    )
    for raw_model, occurred_at, expected_canonical in historical_samples:
        canonical = unpriced_backfill.resolve_harness_catalog_model(raw_model, book)
        assert canonical == expected_canonical
        priced = quote(canonical, 10_000, 1_000, CacheTokens(), at=occurred_at, prices=book)
        assert priced is not None and priced.cost_usd > 0


def test_observation_backfill_preserves_historical_metric_days_baseline(
    db: psycopg.Connection,
) -> None:
    agent = _agent(db, "hist-baseline")
    book = _versioned_price_book()
    ts = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
    _insert_telemetry_and_obs(
        db,
        agent_id=agent,
        occurred_at=ts,
        model="gemini-4-neon-high",
        tokens_in=1_000_000,
        tokens_out=500_000,
        cost_usd=None,
        source="antigravity",
        tag="baseline-unpriced",
    )
    _ = db.execute(
        "UPDATE agent_metric_days SET cost_usd = 100.0, cost_usd_subscription = 40.0, "
        "unpriced_calls = 5, unpriced_calls_subscription = 3 WHERE agent_id = %s AND day = %s",
        (agent, _OLD_DAY),
    )

    report = unpriced_backfill.run_unpriced_backfill(db, today=_TODAY, prices=book, dry_run=False)
    assert report.observations_priced == 1
    assert db.execute(
        "SELECT cost_usd, cost_usd_subscription, unpriced_calls, unpriced_calls_subscription "
        "FROM agent_metric_days WHERE agent_id = %s AND day = %s",
        (agent, _OLD_DAY),
    ).fetchone() == (Decimal("103.0"), Decimal("43.0"), 4, 2)


def test_concurrent_live_write_and_observation_backfill_lose_no_increments(
    db: psycopg.Connection, db_url: str
) -> None:
    agent = _agent(db, "concurrent-obs")
    book = _versioned_price_book()
    ts = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
    _insert_telemetry_and_obs(
        db,
        agent_id=agent,
        occurred_at=ts,
        model="gemini-4-neon-medium",
        tokens_in=1_000_000,
        tokens_out=500_000,
        tokens_cached=500_000,
        cost_usd=None,
        source="antigravity",
        tag="concurrent-unpriced",
    )
    live_obs = observed_metrics.MetricObservation(
        event_id=9_999_001,
        agent_id=agent,
        occurred_at=ts + timedelta(seconds=5),
        kind="usage",
        model="gemini-4-neon",
        usage_calls=1,
        cost_usd=Decimal("2.5"),
        cost_usd_subscription=Decimal("2.5"),
    )
    barrier = Barrier(2)

    def run_backfill_worker() -> int:
        with psycopg.connect(db_url, autocommit=True) as conn:
            barrier.wait(timeout=10)
            return unpriced_backfill.run_unpriced_backfill(
                conn, today=_TODAY, prices=book, dry_run=False
            ).observations_priced

    def run_live_writer() -> int:
        with psycopg.connect(db_url, autocommit=False) as conn:
            barrier.wait(timeout=10)
            inserted = observed_metrics.write_observations([live_obs], db=conn)
            conn.commit()
            return inserted

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_backfill = pool.submit(run_backfill_worker)
        f_live = pool.submit(run_live_writer)
        assert f_backfill.result(timeout=15) == 1
        assert f_live.result(timeout=15) == 1

    assert db.execute(
        "SELECT cost_usd, cost_usd_subscription, unpriced_calls, unpriced_calls_subscription "
        "FROM agent_metric_days WHERE agent_id = %s AND day = %s",
        (agent, _RECENT_DAY),
    ).fetchone() == (Decimal("9.6"), Decimal("9.6"), 0, 0)


def test_ledger_backfill_protects_estimated_rows_per_model_guard_and_sibling_agents(
    db: psycopg.Connection,
) -> None:
    other_agent = _agent(db, "sibling-priced")
    mixed_agent = _agent(db, "mixed-ledger")
    book = _versioned_price_book()
    ts = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)

    _ = db.execute(
        "INSERT INTO agent_model_tokens_daily (agent_id, day, model, llm_calls, tokens_in, "
        "cost_usd, costed_calls, unpriced_calls, estimated_calls) VALUES "
        "(%s, %s, 'sibling-model', 10, 1000, 9.99, 10, 0, 0), "
        "(%s, %s, 'gemini-4-neon', 10, 1000, 2.5, 6, 0, 4), "
        "(%s, %s, 'partial-priced', 50, 5000, 12.0, 50, 0, 0), "
        "(%s, %s, 'claude-fable-5-2[1m]', 1, 1000000, 0.0, 0, 1, 0)",
        (
            other_agent,
            _OLD_DAY,
            mixed_agent,
            _OLD_DAY,
            mixed_agent,
            _OLD_DAY,
            mixed_agent,
            _OLD_DAY,
        ),
    )
    _insert_telemetry_and_obs(
        db,
        agent_id=mixed_agent,
        occurred_at=ts,
        model="gemini-4-neon",
        tokens_in=100,
        tokens_out=10,
        cost_usd=None,
        source="antigravity",
        tag="estimated-raw",
    )
    _insert_telemetry_and_obs(
        db,
        agent_id=mixed_agent,
        occurred_at=ts + timedelta(seconds=1),
        model="partial-priced",
        tokens_in=100,
        tokens_out=10,
        cost_usd=0.1,
        source="antigravity",
        tag="partial-raw",
    )
    _insert_telemetry_and_obs(
        db,
        agent_id=mixed_agent,
        occurred_at=ts + timedelta(seconds=2),
        model="claude-fable-5-2[1m]",
        tokens_in=1_000_000,
        tokens_out=0,
        cost_usd=None,
        source=None,
        tag="fable-unpriced",
    )

    report = unpriced_backfill.run_unpriced_backfill(db, today=_TODAY, prices=book, dry_run=False)
    assert (report.ledger_days_updated, report.ledger_calls_priced) == (1, 1)

    sibling = db.execute(
        "SELECT model, llm_calls, cost_usd, costed_calls, unpriced_calls, estimated_calls "
        "FROM agent_model_tokens_daily WHERE agent_id = %s AND day = %s",
        (other_agent, _OLD_DAY),
    ).fetchall()
    assert sibling == [("sibling-model", 10, pytest.approx(9.99), 10, 0, 0)]

    expected_mixed = [
        ("claude-fable-5-2", 1, pytest.approx(5.0), 1, 0, 0),
        ("gemini-4-neon", 10, pytest.approx(2.5), 6, 0, 4),
        ("partial-priced", 50, pytest.approx(12.0), 50, 0, 0),
    ]
    mixed_rows = db.execute(
        "SELECT model, llm_calls, cost_usd, costed_calls, unpriced_calls, estimated_calls "
        "FROM agent_model_tokens_daily WHERE agent_id = %s AND day = %s ORDER BY model",
        (mixed_agent, _OLD_DAY),
    ).fetchall()
    assert mixed_rows == expected_mixed

    rollup.roll_day(db, _OLD_DAY, prices=book)
    after_rollup = db.execute(
        "SELECT model, llm_calls, cost_usd, costed_calls, unpriced_calls, estimated_calls "
        "FROM agent_model_tokens_daily WHERE agent_id = %s AND day = %s ORDER BY model",
        (mixed_agent, _OLD_DAY),
    ).fetchall()
    assert after_rollup == expected_mixed


def test_concurrent_ledger_backfill_same_day_serializes_without_unique_violation(
    db: psycopg.Connection, db_url: str
) -> None:
    agent = _agent(db, "concurrent-ledger")
    book = _versioned_price_book()
    _seed_two_period_scenario(db, agent)
    barrier = Barrier(2)

    def run_worker() -> unpriced_backfill.UnpricedBackfillReport:
        with psycopg.connect(db_url, autocommit=True) as conn:
            barrier.wait(timeout=10)
            return unpriced_backfill.run_unpriced_backfill(
                conn, today=_TODAY, prices=book, dry_run=False
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(run_worker)
        second_future = pool.submit(run_worker)
        reports = [first_future.result(timeout=15), second_future.result(timeout=15)]

    assert sum(r.ledger_days_updated for r in reports) == 2
    assert sum(r.ledger_calls_priced for r in reports) == 3
    _assert_backfilled_tables(db, agent)


def test_permanently_unpriced_ledger_day_skips_writes_and_reports_zero_updates(
    db: psycopg.Connection,
) -> None:
    agent = _agent(db, "perm-unpriced")
    book = _versioned_price_book()
    ts = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)
    _insert_telemetry_and_obs(
        db,
        agent_id=agent,
        occurred_at=ts,
        model="unpriced-forever",
        tokens_in=10_000,
        tokens_out=1_000,
        cost_usd=None,
        source="antigravity",
        tag="perm-unpriced",
    )
    rollup.roll_day(db, _RECENT_DAY)

    report = unpriced_backfill.run_unpriced_backfill(db, today=_TODAY, prices=book, dry_run=False)
    assert report.ledger_days_scanned == 1
    assert report.ledger_days_updated == 0
    assert report.ledger_calls_priced == 0
    assert report.totals_rebuilt is False


def test_ledger_backfill_merges_priced_and_unpriced_calls_under_same_suffixed_model(
    db: psycopg.Connection,
) -> None:
    agent = _agent(db, "mixed-suffix")
    book = _versioned_price_book()
    ts = datetime(2026, 11, 15, 11, 0, tzinfo=UTC)
    _insert_telemetry_and_obs(
        db,
        agent_id=agent,
        occurred_at=ts,
        model="gemini-4-neon-high",
        tokens_in=1_000_000,
        tokens_out=500_000,
        cost_usd=0.5,
        source="antigravity",
        tag="suffix-priced",
    )
    _insert_telemetry_and_obs(
        db,
        agent_id=agent,
        occurred_at=ts + timedelta(seconds=1),
        model="gemini-4-neon-high",
        tokens_in=1_000_000,
        tokens_out=500_000,
        cost_usd=None,
        source="antigravity",
        tag="suffix-unpriced",
    )
    rollup.roll_day(db, _OLD_DAY)

    report = unpriced_backfill.run_unpriced_backfill(db, today=_TODAY, prices=book, dry_run=False)
    assert (report.ledger_days_updated, report.ledger_calls_priced) == (1, 1)
    assert db.execute(
        "SELECT model, llm_calls, costed_calls, unpriced_calls, estimated_calls, cost_usd "
        "FROM agent_model_tokens_daily WHERE agent_id = %s AND day = %s",
        (agent, _OLD_DAY),
    ).fetchall() == [("gemini-4-neon", 2, 2, 0, 0, pytest.approx(3.5))]
