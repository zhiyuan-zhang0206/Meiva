# meiva:note
# The schedule-manager daemon: its two resident loops and the order it takes resources up and gives them back.
#
# The loops run over the real manager, the home's real pty-sessions service and the test's
# Postgres. A loop's crash is a real database error: a trigger makes the delete that consumes a
# request fail, which is the one write the request loop cannot survive.

from __future__ import annotations

import asyncio
import importlib.util
import socket
from collections.abc import Callable, Generator
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import base.db
import gateway.app
from base.clock import SYSTEM_TIMERS
from base.config import ConfigBoot
from base.daemon.endpoints import ServiceEndpoints
from base.daemon.loop_health import LivenessGroup
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.host.env.registry import process_environment
from base.native_process.loaded_commit import LoadedCommit
from base.sessions.backend import get_shell_backend
from base.sessions.env_forwarding import forward_env_dict
from base.sessions.pty.tests.pty_service import PtyServiceProcess
from base.sessions.pty.tests.pty_service import pty_service as pty_service
from gateway.schedules.tests.db_faults import failing_write
from services.wake.schedule_manager import daemon
from services.wake.schedule_manager.tests.manager_support import schedule_manager


@pytest.fixture
def pool(db_url: str, *, database_gate: ProcessDbGate) -> Generator[ConnectionPool]:
    p = base.db.pool(max_size=4, gate=database_gate)
    try:
        yield p
    finally:
        p.close()


def _queue(conn: psycopg.Connection, schedule_id: int) -> None:
    conn.execute("INSERT INTO schedule_sync_requests (schedule_id) VALUES (%s)", (schedule_id,))
    conn.commit()


def _queued(conn: psycopg.Connection) -> list[int]:
    conn.rollback()
    return [r[0] for r in conn.execute("SELECT schedule_id FROM schedule_sync_requests")]


async def _until(condition: Callable[[], bool], what: str) -> None:
    for _ in range(600):
        if condition():
            return
        await asyncio.sleep(0.025)
    raise AssertionError(f"never happened: {what}")


def test_the_gateway_owns_no_schedule_manager() -> None:
    assert importlib.util.find_spec("gateway.schedules.manager") is None
    assert not hasattr(gateway.app, "ScheduleManager")


async def test_both_loops_run_their_rounds(
    db_conn: psycopg.Connection, pool: ConnectionPool, pty_service: PtyServiceProcess
) -> None:
    # meiva:note
    # a deleted schedule: the request loop reaps its (absent) session
    _queue(db_conn, 4242)
    liveness = LivenessGroup(timers=SYSTEM_TIMERS)
    manager = schedule_manager(
        pool, get_shell_backend(), launch_env=forward_env_dict(process_environment())
    )

    task = asyncio.create_task(daemon.run_loops(pool, manager, liveness))
    try:
        await _until(lambda: _queued(db_conn) == [], "the request loop consumed the request")
        await _until(
            lambda: liveness.snapshot()["reconcile"]["last_success_at"] is not None,
            "the reconcile loop finished a round",
        )
        assert set(liveness.snapshot()) == {"reconcile", "requests"}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_a_crashing_loop_cancels_its_sibling_and_ends_the_service(
    db_conn: psycopg.Connection, pool: ConnectionPool, pty_service: PtyServiceProcess
) -> None:
    _queue(db_conn, 4242)
    manager = schedule_manager(
        pool, get_shell_backend(), launch_env=forward_env_dict(process_environment())
    )

    with (
        failing_write(db_conn, table="schedule_sync_requests", events="DELETE"),
        pytest.raises(ExceptionGroup) as raised,
    ):
        await asyncio.wait_for(
            daemon.run_loops(pool, manager, LivenessGroup(timers=SYSTEM_TIMERS)), timeout=30
        )

    assert [type(exc) for exc in raised.value.exceptions] == [psycopg.errors.RaiseException]
    assert _queued(db_conn) == [4242]


def _no_builtins(config: ConfigBoot) -> ConfigBoot:
    config.set_field("provision_builtin_schedules", False)
    return config


async def test_a_crash_leaves_run_after_releasing_its_resources(
    db_conn: psycopg.Connection,
    pty_service: PtyServiceProcess,
    database: Database,
    unit_home: Path,
    *,
    process_config: ConfigBoot,
) -> None:
    config = _no_builtins(process_config)
    endpoint = ServiceEndpoints.from_boot(config, runtime=unit_home / "run").of("schedule_manager")
    _queue(db_conn, 4242)

    with (
        failing_write(db_conn, table="schedule_sync_requests", events="DELETE"),
        pytest.raises(ExceptionGroup),
    ):
        await asyncio.wait_for(
            daemon.run(
                process_config=config,
                runtime=unit_home / "run",
                database=lambda: database,
                image=LoadedCommit(Path(), None),
            ),
            timeout=30,
        )

    assert not endpoint.pidfile.exists()
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", endpoint.health_port)) != 0, "healthz still listening"


async def test_a_foreign_checkout_refuses_to_supervise_schedules(
    database: Database, unit_home: Path, *, process_config: ConfigBoot
) -> None:
    # meiva:note
    # a prod home whose checkout is not this one
    (unit_home / "source").mkdir()

    with pytest.raises(RuntimeError, match="schedule supervision refused"):
        await daemon.run(
            process_config=process_config,
            runtime=unit_home / "run",
            database=lambda: database,
            image=LoadedCommit(Path(), None),
        )
