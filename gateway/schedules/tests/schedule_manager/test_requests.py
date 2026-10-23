# meiva:note
# The sync-request queue between the API and the schedule manager.
#
# A request is a row the API queues and the manager's request loop consumes. The manager here is
# the real one over a fake session backend, so what a request converges to (a reap, or a reap that
# survives) decides whether its row is consumed.

from __future__ import annotations

import asyncio
import logging
from collections.abc import Generator
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import base.db
from base.cluster import session_name
from base.db.code_version_gate import ProcessDbGate
from base.deploy.lifecycle import start_serving
from base.paths.tests.homes import unit_home as unit_home
from gateway.schedules import session_control
from services.wake.schedule_manager import requests
from services.wake.schedule_manager.tests.manager_support import NOW, schedule_manager
from services.wake.schedule_manager.tests.session_fakes import Backend


@pytest.fixture
def pool(*, database_gate: ProcessDbGate) -> Generator[ConnectionPool]:
    p = base.db.pool(max_size=2, gate=database_gate)
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def backend(serving_root: start_serving.RootBirth, unit_home: Path) -> Backend:
    generation = start_serving.begin_start(unit_home)
    assert start_serving.mark_serving(unit_home, generation, runtime=serving_root.runtime)
    return Backend()


def _queue(conn: psycopg.Connection, *schedule_ids: int) -> None:
    for sid in schedule_ids:
        conn.execute("INSERT INTO schedule_sync_requests (schedule_id) VALUES (%s)", (sid,))
    conn.commit()


def _queued(conn: psycopg.Connection) -> list[int]:
    rows = conn.execute("SELECT schedule_id FROM schedule_sync_requests ORDER BY 1").fetchall()
    return [r[0] for r in rows]


def test_a_queued_request_is_synced_and_removed(
    db_conn: psycopg.Connection, pool: ConnectionPool, backend: Backend
) -> None:
    for sid in (7, 3):
        # meiva:note
        # deleted schedules' sessions
        backend.hold(session_name(f"schedule-{sid}"), "orphan")
    _queue(db_conn, 7, 3)

    handled = requests.consume_requests(pool, schedule_manager(pool, backend), now=NOW)

    assert handled == 2
    assert sorted(backend.killed) == [session_name("schedule-3"), session_name("schedule-7")]
    assert _queued(db_conn) == []


def test_a_request_for_a_deleted_schedule_is_still_synced(
    db_conn: psycopg.Connection, pool: ConnectionPool, backend: Backend
) -> None:
    backend.hold(session_name("schedule-999999"), "orphan")
    _queue(db_conn, 999_999)

    requests.consume_requests(pool, schedule_manager(pool, backend), now=NOW)

    assert backend.killed == [session_name("schedule-999999")]
    assert _queued(db_conn) == []


def test_a_newer_request_made_during_the_sync_survives(
    db_conn: psycopg.Connection, pool: ConnectionPool, backend: Backend
) -> None:
    backend.hold(session_name("schedule-5"), "orphan")
    _queue(db_conn, 5)

    def refresh(_name: str) -> None:
        db_conn.execute(
            "UPDATE schedule_sync_requests SET requested_at = clock_timestamp() "
            "WHERE schedule_id = 5"
        )
        db_conn.commit()

    backend.during_kill = refresh

    requests.consume_requests(pool, schedule_manager(pool, backend), now=NOW)

    assert backend.killed == [session_name("schedule-5")]
    assert _queued(db_conn) == [5]


def test_requests_stay_queued_while_a_sync_is_not_converging(
    db_conn: psycopg.Connection, pool: ConnectionPool, backend: Backend
) -> None:
    for sid in (1, 2):
        backend.hold(session_name(f"schedule-{sid}"), "orphan")
    backend.fail_reap = True
    _queue(db_conn, 1, 2)
    manager = schedule_manager(pool, backend)

    assert requests.consume_requests(pool, manager, now=NOW) == 0
    assert _queued(db_conn) == [1, 2]

    backend.fail_reap = False
    assert requests.consume_requests(pool, manager, now=NOW + 1) == 2
    assert _queued(db_conn) == []


async def test_the_api_side_queues_one_row_and_waits_until_it_is_consumed(
    db_conn: psycopg.Connection, pool: ConnectionPool, backend: Backend
) -> None:
    backend.hold(session_name("schedule-11"), "orphan")
    manager = schedule_manager(pool, backend)

    async def consumer() -> None:
        while not _queued(db_conn):
            await asyncio.sleep(0.01)
        await asyncio.to_thread(lambda: requests.consume_requests(pool, manager, now=NOW))

    task = asyncio.create_task(consumer())
    await asyncio.wait_for(
        session_control.request_sync(pool, 11, wait_s=8.0, poll_s=0.05), timeout=10
    )
    await task

    assert backend.killed == [session_name("schedule-11")]
    assert _queued(db_conn) == []


async def test_the_api_side_gives_up_waiting_when_no_service_consumes(
    db_conn: psycopg.Connection, pool: ConnectionPool, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=session_control.__name__):
        await session_control.request_sync(pool, 12, wait_s=0.3, poll_s=0.05)

    assert _queued(db_conn) == [12]
    assert "not consumed" in caplog.text


def test_repeated_requests_for_one_schedule_collapse_to_one_row(
    db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    session_control.enqueue_blocking(pool, 4)
    session_control.enqueue_blocking(pool, 4)

    assert _queued(db_conn) == [4]
