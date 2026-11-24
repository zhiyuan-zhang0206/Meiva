from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from base.clock.tests.fakes import system_clock
from base.db import Database
from gateway.inspect import _metrics
from gateway.inspect.router import InspectAppState, build_query_cache, router
from gateway.inspect.schemas import AgentInspectStatistics
from gateway.inspect.tests.rows import returned_id


@pytest.fixture
def app(database: Database) -> Iterator[FastAPI]:
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


def _agent(conn: psycopg.Connection, born: datetime) -> int:
    agent_id = returned_id(conn, "INSERT INTO agents(label) VALUES ('metrics') RETURNING id")
    conn.execute(
        "INSERT INTO agents_meta(id,spawner,status,spawned_at) VALUES (%s,'user','running',%s)",
        (agent_id, born),
    )
    return agent_id


def _usage(
    db: psycopg.Connection,
    agent_id: int,
    at: datetime,
    *,
    calls: int = 1,
    billing: str = "api",
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cache_read: int | None = None,
    cost_usd: float | None = None,
) -> None:
    db.execute(
        """
        INSERT INTO llm_usage_ledger
            (call_id, occurred_at, agent_id, machine, cluster, billing, usage_kind,
             model, calls, complete, tokens_in, tokens_out, cache_read,
             cost_usd, price_source, price_snapshot)
        VALUES (gen_random_uuid()::text, %s, %s, 'test', 'test', %s, 'test', 'm', %s, TRUE,
                %s, %s, %s, %s, %s, '{}')
        """,
        (
            at,
            agent_id,
            billing,
            calls,
            tokens_in,
            tokens_out,
            cache_read,
            cost_usd,
            "live" if cost_usd is not None else None,
        ),
    )


def test_usage_is_agent_scoped_and_an_unpriced_call_is_counted_not_priced(
    db_conn: psycopg.Connection,
) -> None:
    end = datetime.now(UTC)
    start = end - timedelta(hours=1)
    aid = _agent(db_conn, start)
    other = _agent(db_conn, start)
    _usage(
        db_conn,
        aid,
        start + timedelta(seconds=1),
        tokens_in=123,
        tokens_out=50,
        cache_read=50,
        cost_usd=0.123456,
    )
    _usage(db_conn, aid, start + timedelta(seconds=2), tokens_out=25)
    _usage(
        db_conn, other, start + timedelta(seconds=1), calls=10000, tokens_out=10**6, cost_usd=100000
    )
    cost = _metrics.read_cost(db_conn, aid, start, end)
    assert cost.cost_usd == 0.1235
    assert cost.llm_calls == 2
    assert cost.unpriced_calls == 1
    assert cost.tokens_out == 75
    assert cost.cache_hit_pct == 40.65


def test_cost_splits_into_api_and_subscription_buckets(db_conn: psycopg.Connection) -> None:
    day = datetime(2026, 9, 15, tzinfo=UTC)  # time-bomb-ok: explicit fixture window input
    aid = _agent(db_conn, day - timedelta(days=2))
    _usage(db_conn, aid, day + timedelta(hours=1), cost_usd=0.25)
    _usage(db_conn, aid, day + timedelta(hours=2), calls=2, billing="subscription", cost_usd=1.5)
    _usage(db_conn, aid, day + timedelta(hours=3), billing="subscription")
    cost = _metrics.read_cost(db_conn, aid, day, day + timedelta(days=1))
    assert (cost.cost_usd, cost.cost_usd_api, cost.cost_usd_subscription) == (1.75, 0.25, 1.5)
    assert (cost.unpriced_calls, cost.unpriced_calls_api, cost.unpriced_calls_subscription) == (
        1,
        0,
        1,
    )
    assert (cost.tokens_in, cost.llm_calls) == (0, 4)


def test_the_window_is_half_open(db_conn: psycopg.Connection) -> None:
    end = datetime.now(UTC)
    start = end - timedelta(hours=1)
    aid = _agent(db_conn, start - timedelta(days=1))
    _usage(db_conn, aid, start - timedelta(microseconds=1), tokens_in=1)
    _usage(db_conn, aid, start, tokens_in=10)
    _usage(db_conn, aid, end - timedelta(microseconds=1), tokens_in=100)
    _usage(db_conn, aid, end, tokens_in=1000)
    assert _metrics.read_cost(db_conn, aid, start, end).tokens_in == 110


def test_large_integer_counters_do_not_round_through_float(db_conn: psycopg.Connection) -> None:
    end = datetime.now(UTC)
    start = end - timedelta(hours=1)
    aid = _agent(db_conn, start)
    exact = 2**53 + 1
    _usage(db_conn, aid, start, tokens_out=exact)
    assert _metrics.read_cost(db_conn, aid, start, end).tokens_out == exact


@pytest.mark.parametrize("cached", [None, 0, 800])
def test_cache_rate_uses_only_metered_calls(
    db_conn: psycopg.Connection, cached: int | None
) -> None:
    end = datetime.now(UTC)
    start = end - timedelta(hours=1)
    aid = _agent(db_conn, start)
    _usage(
        db_conn, aid, start + timedelta(seconds=1), tokens_in=1000, cache_read=cached, cost_usd=1
    )
    _usage(db_conn, aid, start + timedelta(seconds=2), tokens_in=5000, cost_usd=2)
    cost = _metrics.read_cost(db_conn, aid, start, end)
    assert cost.tokens_in == 6000
    assert cost.llm_calls == 2
    assert cost.cost_usd == 3
    assert cost.cache_hit_pct == (None if cached is None else cached / 10)


def test_statistics_http_serves_the_ledger_usage(app: FastAPI, db_conn: psycopg.Connection) -> None:
    now = datetime.now(UTC)
    aid = _agent(db_conn, now - timedelta(hours=2))
    _usage(db_conn, aid, now - timedelta(hours=1), tokens_in=7, cost_usd=0.5)
    db_conn.commit()
    with TestClient(app) as client:
        response = client.get(f"/api/agents/{aid}/inspect/statistics")
    assert response.status_code == 200, response.text
    body = AgentInspectStatistics.model_validate_json(response.text)
    assert set(body.model_dump()) == {"agent_id", "window_hours", "applied_window_hours", "cost"}
    assert (body.cost.tokens_in, body.cost.cost_usd) == (7, 0.5)
