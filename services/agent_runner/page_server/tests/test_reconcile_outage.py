# meiva:note
# A reconcile round that could not reach the database is a fact about the database, not about
# this process: the daemon owns every page session on the machine, so ending the loop over a
# transient stall kills sessions that a restart cannot bring back. These tests pin both
# directions of that split and the two bounds around it — the outage budget that still ends a
# sustained outage, and the health payload that reports a failing loop without asking for a
# restart (a restart cannot repair an unreachable database).

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import cast

import psycopg
import pytest
from psycopg_pool import ConnectionPool, PoolTimeout

from base.clock import SYSTEM_TIMERS
from base.cluster.machine import MachineIdentityReader
from base.daemon import health_schema
from base.daemon.health_schema import OK
from base.daemon.loop_health import LivenessGroup, LoopProgress
from base.events.live.bus import EventBus
from services.agent_runner.page_server import daemon
from services.agent_runner.page_server.config import PageServerConfig

_POLL_S = 0.01


# meiva:note
# Every borrow fails with `error`, and counts how often the loop tried: the loop only reaches the
# database through `open_rows` -> `pool.connection()`, so this is the whole surface it needs.
class _FailingPool:
    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.attempts = 0

    def connection(self) -> object:
        self.attempts += 1
        raise self._error


class _Machine:
    def name(self) -> str:
        return "test-machine"

    def host(self) -> str:
        return "127.0.0.1"

    def role(self) -> object:
        return ()

    def description(self) -> str | None:
        return None


def _config(*, exit_seconds: float = 300.0) -> PageServerConfig:
    return PageServerConfig(
        page_server_poll_interval_seconds=_POLL_S,
        heartbeat_interval_seconds=600.0,
        page_server_db_failure_exit_seconds=exit_seconds,
    )


def _liveness() -> tuple[LivenessGroup, LoopProgress]:
    group = LivenessGroup(timers=SYSTEM_TIMERS)
    return group, group.register("reconcile", 60.0)


async def _run(
    pool: _FailingPool, progress: LoopProgress, home: Path, config: PageServerConfig
) -> None:
    await daemon.reconcile_loop(
        cast(ConnectionPool, pool),
        progress,
        config,
        EventBus.from_settings(rng=secrets.SystemRandom(), timers=SYSTEM_TIMERS),
        local_machine=cast(MachineIdentityReader, _Machine()),
        home=home,
    )


async def _until(predicate: Callable[[], bool]) -> None:
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(_POLL_S)
    raise AssertionError("the condition never held")


async def test_a_pool_timeout_skips_the_round_and_the_loop_keeps_going(
    tmp_path: Path,
) -> None:
    pool = _FailingPool(PoolTimeout("couldn't get a connection after 30.00 sec"))
    _group, progress = _liveness()
    task = asyncio.create_task(_run(pool, progress, tmp_path, _config()))
    try:
        await _until(lambda: pool.attempts >= 3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert progress.consecutive_failures >= 3
    assert (progress.last_error_message() or "").startswith("PoolTimeout: ")


async def test_a_programming_error_still_ends_the_loop(tmp_path: Path) -> None:
    pool = _FailingPool(psycopg.ProgrammingError("column session_name does not exist"))
    _group, progress = _liveness()

    with pytest.raises(psycopg.ProgrammingError):
        await asyncio.wait_for(_run(pool, progress, tmp_path, _config()), timeout=10)

    assert pool.attempts == 1


async def test_a_sustained_outage_exits_after_the_configured_seconds(
    tmp_path: Path,
) -> None:
    pool = _FailingPool(PoolTimeout("couldn't get a connection after 30.00 sec"))
    _group, progress = _liveness()

    with pytest.raises(daemon.DatabaseOutageError, match="consecutive rounds"):
        await asyncio.wait_for(
            _run(pool, progress, tmp_path, _config(exit_seconds=0.05)), timeout=10
        )

    assert pool.attempts >= 2


def test_a_failing_loop_is_reported_without_asking_for_a_restart() -> None:
    group, progress = _liveness()
    progress.mark_error("PoolTimeout: couldn't get a connection after 30.00 sec")
    progress.mark_error("PoolTimeout: couldn't get a connection after 30.00 sec")

    records = daemon.health_components(group)
    record = {str(item["name"]): item for item in records}["reconcile"]

    assert record["status"] == OK
    assert record["progress"] == "2 consecutive failed round(s)"
    assert str(record["last_error"]).startswith("PoolTimeout: ")
    assert str(record["detail"]).startswith("last completed round at ")

    status, _body = health_schema.render({"name": "page_server"}, records, liveness=group)
    assert status == 200


def test_a_completed_round_clears_the_failure_count() -> None:
    group, progress = _liveness()
    progress.mark_error("OperationalError: server closed the connection unexpectedly")

    progress.mark_success()

    assert progress.consecutive_failures == 0
    assert daemon.health_components(group)[0]["progress"] == "0 consecutive failed round(s)"
