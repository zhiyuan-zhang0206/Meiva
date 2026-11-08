"""Persisted metric reads: evidence, source seams, exact arithmetic, and windows."""

from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from base.clock.tests.fakes import system_clock
from base.db import Database
from base.db.rows import query_one
from base.log.tests.log_records import LogRecords
from base.telemetry.metrics.observed_metrics import MetricObservation, write_observations
from gateway.inspect import _metrics
from gateway.inspect.router import InspectAppState, build_query_cache, router
from gateway.inspect.tests.rows import returned_id


@pytest.fixture
def app(database: Database) -> Iterator[FastAPI]:
    """Exercise the owning inspector router with a real isolated database."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncGenerator[None]:
        queries = build_query_cache()
        application.state.inspect_query_cache = queries
        try:
            yield
        finally:
            await queries.aclose()

    application = FastAPI(lifespan=lifespan)
    application.state = InspectAppState()
    application.include_router(router)
    application.state.clock = system_clock()
    with database.pool(max_size=2) as pool:
        application.state.db_pool = pool
        yield application


# meiva:note
# The birth time of one agent.
@dataclass(frozen=True)
class _SpawnedRow:
    spawned_at: datetime


def _agent(conn: psycopg.Connection, born: datetime) -> int:
    agent_id = returned_id(conn, "INSERT INTO agents(label) VALUES ('metrics') RETURNING id")
    conn.execute(
        "INSERT INTO agents_meta(id,spawner,status,spawned_at) VALUES (%s,'user','running',%s)",
        (agent_id, born),
    )
    return agent_id


def _read(
    conn: psycopg.Connection,
    agent_id: int,
    start: datetime,
    end: datetime,
    *,
    collection: datetime | None = None,
) -> _metrics.MetricsSnapshot:
    born = query_one(
        conn, _SpawnedRow, "SELECT spawned_at FROM agents_meta WHERE id=%s", (agent_id,)
    )
    assert born is not None
    return _metrics.read_snapshot(
        conn, agent_id, start, end, born.spawned_at, collection or start - timedelta(seconds=1)
    )


def test_numeric_facts_are_agent_scoped_and_prices_are_never_recomputed(
    db_conn: psycopg.Connection,
) -> None:
    end = datetime.now(UTC)
    start = end - timedelta(hours=1)
    aid = _agent(db_conn, start)
    other = _agent(db_conn, start)
    write_observations(
        [
            MetricObservation(
                1,
                aid,
                start + timedelta(seconds=1),
                "usage",
                model="removed-model",
                usage_calls=1,
                tokens_in=123,
                tokens_out=50,
                tokens_cached=50,
                cost_usd=Decimal("0.123456"),
            ),
            MetricObservation(
                2,
                aid,
                start + timedelta(seconds=2),
                "usage",
                usage_calls=1,
                unpriced_calls=1,
                tokens_out=25,
            ),
            MetricObservation(
                3,
                other,
                start + timedelta(seconds=1),
                "usage",
                usage_calls=10000,
                tokens_out=1000000,
                cost_usd=Decimal("100000"),
            ),
        ],
        db=db_conn,
    )
    snapshot = _read(db_conn, aid, start, end)
    assert snapshot.cost is not None
    assert snapshot.cost.cost_usd == 0.1235
    assert snapshot.cost.llm_calls == 2
    assert snapshot.cost.unpriced_calls == 1
    assert snapshot.cost.tokens_out == 75
    assert snapshot.cost.cache_hit_pct == 40.65
    assert snapshot.metadata.collection == "observed"


def test_cost_splits_into_api_and_subscription_buckets(db_conn: psycopg.Connection) -> None:
    # meiva:note
    # One agent can run on both billing modes (a restart can move it to another harness), so the
    # cost read keeps the metered and subscription-covered parts apart and still sums the total.
    # A full past UTC day reads the day table; the edges read raw observations.
    # A fixed past UTC day is the fixture input: the read must cover a whole day for the
    # day-summary path, and the assertion is about the split, not about "now".
    day = datetime(2026, 9, 15, tzinfo=UTC)  # time-bomb-ok: explicit fixture window input
    aid = _agent(db_conn, day - timedelta(days=2))
    write_observations(
        [
            MetricObservation(
                1, aid, day + timedelta(hours=1), "usage", usage_calls=1, cost_usd=Decimal("0.25")
            ),
            MetricObservation(
                2,
                aid,
                day + timedelta(hours=2),
                "usage",
                usage_calls=2,
                cost_usd=Decimal("1.5"),
                cost_usd_subscription=Decimal("1.5"),
            ),
            MetricObservation(
                3,
                aid,
                day + timedelta(hours=3),
                "usage",
                usage_calls=1,
                unpriced_calls=1,
                unpriced_calls_subscription=1,
            ),
        ],
        db=db_conn,
    )
    snapshot = _read(db_conn, aid, day, day + timedelta(days=1), collection=day - timedelta(days=1))
    cost = snapshot.cost
    assert cost is not None
    assert (cost.cost_usd, cost.cost_usd_api, cost.cost_usd_subscription) == (1.75, 0.25, 1.5)
    assert (cost.unpriced_calls, cost.unpriced_calls_api, cost.unpriced_calls_subscription) == (
        1,
        0,
        1,
    )
    assert (cost.tokens_in, cost.llm_calls) == (0, 4)


def test_half_open_window_has_exact_float_quantiles(db_conn: psycopg.Connection) -> None:
    end = datetime.now(UTC)
    start = end - timedelta(hours=1)
    aid = _agent(db_conn, start)
    write_observations(
        [
            MetricObservation(
                1, aid, start, "turn", turn_total=1, turn_ok=1, turn_duration_seconds=0.1
            ),
            MetricObservation(
                2, aid, end - timedelta(seconds=1), "turn", turn_total=1, turn_duration_seconds=0.9
            ),
            MetricObservation(
                3, aid, end, "turn", turn_total=1, turn_ok=1, turn_duration_seconds=900
            ),
        ],
        db=db_conn,
    )
    snapshot = _read(db_conn, aid, start, end)
    assert snapshot.stats is not None
    assert snapshot.stats.turn_total == 2
    assert snapshot.stats.turn_ok == 1
    assert snapshot.stats.turn_p50_seconds == 0.5
    assert snapshot.stats.turn_p90_seconds == 0.82
    assert snapshot.stats.turn_min_seconds == 0.1
    assert snapshot.stats.turn_max_seconds == 0.9
    assert snapshot.metadata.turns.duration_precision == "exact"


def test_legacy_and_observed_days_never_double_count_and_backfill_fills_gaps(
    db_conn: psycopg.Connection,
) -> None:
    midnight = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    old = midnight - timedelta(days=3)
    aid = _agent(db_conn, old)
    db_conn.execute(
        "INSERT INTO agent_model_tokens_daily(agent_id,day,model,llm_calls,cost_usd,tokens_out) "
        "VALUES (%s,%s,'old',2,7.5,100)",
        (aid, old.date()),
    )
    db_conn.execute(
        "INSERT INTO agent_metrics_daily(agent_id,day,turn_total,turn_ok,turn_dur_sum,turn_dur_min,turn_dur_max,turn_dur_hist) "
        'VALUES (%s,%s,2,1,2.4,0.8,1.6,\'{"0":1,"1":1}\')',
        (aid, old.date()),
    )
    write_observations(
        [
            MetricObservation(
                1,
                aid,
                old + timedelta(hours=1),
                "usage",
                usage_calls=1,
                cost_usd=Decimal("7.5"),
                tokens_out=100,
            ),
            MetricObservation(
                2,
                aid,
                old + timedelta(hours=1),
                "turn",
                turn_total=1,
                turn_ok=1,
                turn_duration_seconds=0.8,
            ),
            MetricObservation(
                3,
                aid,
                old + timedelta(days=1, hours=1),
                "usage",
                usage_calls=1,
                cost_usd=Decimal("2.5"),
                tokens_out=20,
            ),
            MetricObservation(
                4,
                aid,
                old + timedelta(days=1, hours=1),
                "turn",
                turn_total=1,
                turn_ok=1,
                turn_duration_seconds=2.5,
            ),
        ],
        db=db_conn,
    )
    result = _read(db_conn, aid, old, midnight, collection=midnight)
    assert result.cost is not None and result.stats is not None
    assert result.cost.cost_usd == 10
    assert result.cost.llm_calls == 3
    assert result.cost.tokens_out == 120
    assert result.stats.turn_total == 3
    assert result.stats.turn_min_seconds == 0.8
    assert result.stats.turn_max_seconds == 2.5
    assert result.metadata.turns.duration_precision == "mixed"
    assert result.metadata.cost.availability == "partial"


def test_partial_old_day_does_not_include_whole_day_ledger(db_conn: psycopg.Connection) -> None:
    midnight = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    old = midnight - timedelta(days=3)
    aid = _agent(db_conn, old)
    db_conn.execute(
        "INSERT INTO agent_model_tokens_daily(agent_id,day,model,llm_calls,cost_usd) VALUES (%s,%s,'old',100,999)",
        (aid, old.date()),
    )
    write_observations(
        [
            MetricObservation(
                1, aid, old + timedelta(hours=3), "usage", usage_calls=1, cost_usd=Decimal("2")
            )
        ],
        db=db_conn,
    )
    result = _read(
        db_conn, aid, old + timedelta(hours=2), old + timedelta(hours=4), collection=midnight
    )
    assert result.cost is not None
    assert result.cost.cost_usd == 2
    assert result.cost.llm_calls == 1


def test_new_days_ignore_old_rollup_writer(db_conn: psycopg.Connection) -> None:
    end = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    start = end - timedelta(days=1)
    aid = _agent(db_conn, start)
    db_conn.execute(
        "INSERT INTO agent_model_tokens_daily(agent_id,day,model,llm_calls,cost_usd) VALUES (%s,%s,'stale',100,999)",
        (aid, start.date()),
    )
    write_observations(
        [MetricObservation(1, aid, start, "usage", usage_calls=1, cost_usd=Decimal("3"))],
        db=db_conn,
    )
    result = _read(db_conn, aid, start, end, collection=start)
    assert result.cost is not None and result.cost.cost_usd == 3


def test_missing_historical_evidence_is_not_zero(db_conn: psycopg.Connection) -> None:
    now = datetime.now(UTC)
    start = now - timedelta(days=20)
    aid = _agent(db_conn, start)
    result = _read(db_conn, aid, start, now, collection=now)
    assert result.cost is None
    assert result.stats is None
    assert result.tps is None
    assert result.activity is None
    assert result.metadata.cost.availability == "unavailable"


def test_state_intervals_exclude_terminated_gaps(db_conn: psycopg.Connection) -> None:
    now = datetime.now(UTC)
    start = now - timedelta(hours=1)
    aid = _agent(db_conn, start)
    db_conn.execute("DELETE FROM agent_lifecycle_intervals WHERE agent_id=%s", (aid,))
    db_conn.execute(
        "INSERT INTO agent_lifecycle_intervals(agent_id,started_at,ended_at) VALUES (%s,%s,%s),(%s,%s,NULL)",
        (aid, start, start + timedelta(minutes=10), aid, start + timedelta(minutes=40)),
    )
    write_observations(
        [
            MetricObservation(
                1, aid, start + timedelta(minutes=5), "activity", active_seconds=60, exec_seconds=20
            )
        ],
        db=db_conn,
    )
    result = _read(db_conn, aid, start, now)
    assert result.activity is not None
    assert result.activity.alive_seconds == 1800
    assert result.activity.active_seconds == 60
    assert result.activity.exec_seconds == 20


def test_statistics_http_serves_the_persisted_sql_read_model(
    app: FastAPI, db_conn: psycopg.Connection
) -> None:
    now = datetime.now(UTC)
    aid = _agent(db_conn, now)
    db_conn.commit()
    with TestClient(app) as client:
        response = client.get(f"/api/agents/{aid}/inspect/statistics")
    assert response.status_code == 200, response.text
    assert response.json()["metadata"]["collection"] == "observed"


# meiva:note
# The coverage note is wired into the statistics read (#3869 review): a
# change that drops the call must fail here, not just in review.
def test_statistics_read_logs_the_coverage_limits(
    app: FastAPI, db_conn: psycopg.Connection, loguru_records: LogRecords
) -> None:
    # meiva:note
    # born before the collection
    aid = _agent(db_conn, datetime.now(UTC) - timedelta(days=10))
    db_conn.commit()
    with TestClient(app) as client:
        response = client.get(f"/api/agents/{aid}/inspect/statistics")
    assert response.status_code == 200, response.text
    limits = [
        extra
        for extra in loguru_records.extras(message="coverage limit")
        if extra["agent_id"] == aid
    ]
    assert {extra["condition"] for extra in limits} == {"historical_coverage_unknown"}


def test_recent_window_for_old_agent_preserves_activity_and_lm_throughput(
    db_conn: psycopg.Connection,
) -> None:
    end = datetime.now(UTC)
    start = end - timedelta(hours=1)
    collection = end - timedelta(hours=2)
    aid = _agent(db_conn, end - timedelta(days=10))
    db_conn.execute("DELETE FROM agent_lifecycle_intervals WHERE agent_id=%s", (aid,))
    db_conn.execute(
        "INSERT INTO agent_lifecycle_intervals(agent_id,started_at) VALUES (%s,%s)",
        (aid, collection),
    )
    write_observations(
        [
            MetricObservation(1, aid, start, "activity", active_seconds=120),
            MetricObservation(2, aid, start, "usage", usage_calls=1, tokens_out=100),
            MetricObservation(3, aid, start, "turn", turn_total=1, turn_duration_seconds=10),
        ],
        db=db_conn,
    )
    result = _read(db_conn, aid, start, end, collection=collection)
    assert result.activity is not None and result.tps is not None
    assert result.activity.alive_seconds == 3600
    assert result.activity.active_seconds == 120
    assert result.tps.lm_stage_tps == 10
    assert result.tps.agent_lifecycle_tps is None
    assert result.metadata.activity.availability == "observed"


def test_missing_duration_is_unknown_throughput_not_zero(db_conn: psycopg.Connection) -> None:
    end = datetime.now(UTC)
    start = end - timedelta(hours=1)
    aid = _agent(db_conn, start)
    write_observations(
        [
            MetricObservation(1, aid, start, "usage", usage_calls=1, tokens_out=100),
            MetricObservation(2, aid, start, "turn", turn_total=1),
        ],
        db=db_conn,
    )
    result = _read(db_conn, aid, start, end)
    assert result.stats is not None and result.tps is not None and result.activity is not None
    assert result.stats.turn_p50_seconds is None
    assert result.tps.lm_stage_tps is None
    assert result.activity.llm_seconds is None
    assert result.metadata.turns.availability == "partial"


def test_large_integer_counters_do_not_round_through_float(db_conn: psycopg.Connection) -> None:
    end = datetime.now(UTC)
    start = end - timedelta(hours=1)
    aid = _agent(db_conn, start)
    exact = 2**53 + 1
    write_observations(
        [MetricObservation(1, aid, start, "usage", usage_calls=1, tokens_out=exact)], db=db_conn
    )
    result = _read(db_conn, aid, start, end)
    assert result.cost is not None and result.cost.tokens_out == exact


def test_replayed_turns_with_missing_duration_cannot_replace_historical_histogram(
    db_conn: psycopg.Connection,
) -> None:
    midnight = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    old = midnight - timedelta(days=2)
    aid = _agent(db_conn, old)
    db_conn.execute(
        'INSERT INTO agent_metrics_daily(agent_id,day,turn_total,turn_ok,turn_dur_sum,turn_dur_min,turn_dur_max,turn_dur_hist) VALUES (%s,%s,2,2,6,2,4,\'{"2":1,"4":1}\')',
        (aid, old.date()),
    )
    write_observations(
        [
            MetricObservation(1, aid, old, "turn", turn_total=1, turn_duration_seconds=2),
            MetricObservation(2, aid, old, "turn", turn_total=1),
        ],
        db=db_conn,
    )
    result = _read(db_conn, aid, old, midnight, collection=midnight)
    assert result.stats is not None
    assert result.stats.turn_p50_seconds == 3
    assert result.metadata.turns.duration_precision == "one_second_buckets"


@pytest.mark.parametrize("full_day", [False, True])
@pytest.mark.parametrize("cached", [None, 0, 800])
def test_observed_cache_rate_uses_only_metered_calls(
    db_conn: psycopg.Connection, cached: int | None, full_day: bool
) -> None:
    start = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    end = start + timedelta(days=1) if full_day else start + timedelta(hours=1)
    aid = _agent(db_conn, start)
    write_observations(
        [
            MetricObservation(
                1,
                aid,
                start + timedelta(seconds=1),
                "usage",
                usage_calls=1,
                tokens_in=1000,
                tokens_cached=cached or 0,
                tokens_cache_input=0 if cached is None else 1000,
                cost_usd=Decimal(1),
            ),
            MetricObservation(
                2,
                aid,
                start + timedelta(seconds=2),
                "usage",
                usage_calls=1,
                tokens_in=5000,
                tokens_cache_input=0,
                cost_usd=Decimal(2),
            ),
        ],
        db=db_conn,
    )
    snapshot = _read(db_conn, aid, start, end)
    assert snapshot.cost is not None
    assert snapshot.cost.tokens_in == 6000
    assert snapshot.cost.llm_calls == 2
    assert snapshot.cost.cost_usd == 3
    assert snapshot.cost.cache_hit_pct == (None if cached is None else cached / 10)
