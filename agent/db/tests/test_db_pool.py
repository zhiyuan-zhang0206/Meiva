# meiva:note
# `LoggingConnectionPool` — a slow or timed-out connection borrow must surface
# in the log instead of being a silent multi-second block.
#
# A transiently unresponsive Postgres used to read as a mystery node stall: the
# only signal was a gap in the agent log while `getconn` silently waited out its
# (default 30s) timeout. These tests pin the logging contract on a real pool over
# the throwaway Postgres: a fast borrow is quiet, a borrow that waited for the one
# connection WARNs, `PoolTimeout` ERRORs and still propagates. The waits are real
# and short (a connection held for a fraction of a second) and the slow threshold is
# a constructor value, so no clock is replaced.
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.db import LoggingConnectionPool, claim_inbound_batch
from base.db import create_agent
from base.events.declarations.agent_runtime import DbPoolAcquireSlow
from base.log.tests.log_records import LogRecords

_HOLD_S = 0.25


@asynccontextmanager
async def _pool(
    db_url: str,
    *,
    slow_acquire_warn_seconds: float = 60.0,
    timeout: float = 30.0,
    check: Callable[[psycopg.AsyncConnection], Awaitable[None]] | None = None,
) -> AsyncGenerator[LoggingConnectionPool[psycopg.AsyncConnection]]:
    async with LoggingConnectionPool[psycopg.AsyncConnection](
        db_url,
        pool_name="ops",
        slow_acquire_warn_seconds=slow_acquire_warn_seconds,
        min_size=0,
        max_size=1,
        timeout=timeout,
        kwargs={"autocommit": True, "prepare_threshold": None},
        check=check,
        open=False,
    ) as pool:
        yield pool


def _acquire_records(records: LogRecords) -> list[dict[str, Any]]:
    return [r for r in records if str(r["extra"].get("event", "")).startswith("db_pool_acquire")]


# meiva:note
# Borrow the pool's one connection while `holder` keeps it for `_HOLD_S` more.
async def _release_after(
    pool: AsyncConnectionPool[psycopg.AsyncConnection], holder: psycopg.AsyncConnection
) -> psycopg.AsyncConnection:
    waiting = asyncio.create_task(pool.getconn())
    await asyncio.sleep(_HOLD_S)
    await pool.putconn(holder)
    return await waiting


# meiva:note
# No per-host coalescing: each slow borrow is one WARNING, so a rule can count them.
async def test_fast_acquire_is_quiet(db_url: str, loguru_records: LogRecords) -> None:
    async with _pool(db_url) as pool:
        conn = await pool.getconn()
        await pool.putconn(conn)

    assert _acquire_records(loguru_records) == []


async def test_slow_acquire_warns_and_names_the_slot_wait(
    db_url: str, loguru_records: LogRecords
) -> None:
    async with _pool(db_url, slow_acquire_warn_seconds=_HOLD_S / 2) as pool:
        holder = await pool.getconn()
        loguru_records.clear()
        conn = await _release_after(pool, holder)
        await pool.putconn(conn)

    [record] = _acquire_records(loguru_records)
    extra = record["extra"]
    assert extra["event"] == "db_pool_acquire_slow"
    assert extra["name"] == "ops"
    assert extra["slot_wait_ms"] >= _HOLD_S * 1000 * 0.8
    assert extra["check_attempts"] == 1
    assert record["level"].name == "WARNING"
    assert set(extra) >= set(DbPoolAcquireSlow.__annotations__)


async def test_every_slow_acquire_warns(db_url: str, loguru_records: LogRecords) -> None:
    async with _pool(db_url, slow_acquire_warn_seconds=0.0) as pool:
        for _ in range(2):
            await pool.putconn(await pool.getconn())

    records = _acquire_records(loguru_records)
    assert [r["extra"]["event"] for r in records] == ["db_pool_acquire_slow"] * 2
    assert [r["level"].name for r in records] == ["WARNING", "WARNING"]


# meiva:note
# A `PoolTimeout` ERRORs (named, with the pool's state) and still raises.
async def test_slow_acquire_separates_slot_wait_and_check(
    db_url: str, loguru_records: LogRecords
) -> None:
    async def slow_check(_conn: psycopg.AsyncConnection) -> None:
        await asyncio.sleep(_HOLD_S)

    async with _pool(db_url, slow_acquire_warn_seconds=_HOLD_S / 2, check=slow_check) as pool:
        holder = await pool.getconn()
        loguru_records.clear()
        conn = await _release_after(pool, holder)
        await pool.putconn(conn)

    extra = _acquire_records(loguru_records)[0]["extra"]
    assert extra["slot_wait_ms"] >= _HOLD_S * 1000 * 0.8
    assert extra["check_ms"] >= _HOLD_S * 1000 * 0.8
    assert extra["check_attempts"] == 1


async def test_pool_timeout_errors_and_propagates(db_url: str, loguru_records: LogRecords) -> None:
    async with _pool(db_url) as pool:
        holder = await pool.getconn()
        loguru_records.clear()

        with pytest.raises(PoolTimeout):
            await pool.getconn(timeout=_HOLD_S)

        await pool.putconn(holder)

    [record] = _acquire_records(loguru_records)
    extra = record["extra"]
    assert extra["event"] == "db_pool_acquire_timeout"
    assert extra["name"] == "ops"
    assert extra["pool_size"] == 1
    assert extra["pool_available"] == 0
    assert extra["mx"] == 1
    assert extra["slot_wait_ms"] >= _HOLD_S * 1000 * 0.8
    assert record["level"].name == "ERROR"


# meiva:note
# A claim round whose pool slot never frees raises `PoolTimeout` at the pool's own
# timeout and leaves nothing checked out.
async def test_claim_waits_for_a_pool_slot_no_longer_than_the_pool_timeout(
    db_url: str,
) -> None:
    async with _pool(db_url, timeout=_HOLD_S) as pool:
        holder = await pool.getconn()

        with pytest.raises(PoolTimeout):
            await claim_inbound_batch(pool, 42, incarnation=None)

        await pool.putconn(holder)
        assert pool.get_stats()["pool_available"] == 1


# meiva:note
# The claim bound also caps waiting on the ownership row lock, and the borrow is released.
async def test_claim_waits_for_the_ownership_row_lock_no_longer_than_the_pool_timeout(
    db_url: str, db_conn: psycopg.Connection
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.execute("INSERT INTO agents_meta (id, status) VALUES (%s, 'idling')", (agent_id,))
    db_conn.commit()
    db_conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,))
    async with _pool(db_url, timeout=_HOLD_S) as pool:
        with pytest.raises(psycopg.errors.LockNotAvailable):
            await claim_inbound_batch(pool, agent_id, incarnation=None)

        db_conn.rollback()
        assert pool.get_stats()["pool_available"] == 1
