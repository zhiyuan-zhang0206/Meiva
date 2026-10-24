from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import base.db
from base.clock import Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers
from base.daemon.loop_health import LoopProgress
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from ops.cluster.rpc import ClusterOpUnreachable
from services.wake.heartbeat import loops as heartbeat_loops
from services.wake.heartbeat.liveness import LivenessConfig, probe_machine, run_liveness_pass


@pytest.fixture
def pool(db_url: str, *, database_gate: ProcessDbGate) -> Iterator[ConnectionPool]:
    p = base.db.pool(max_size=2, gate=database_gate)
    try:
        yield p
    finally:
        p.close()


def _machine(
    conn: psycopg.Connection,
    name: str,
    *,
    staging: bool = False,
    stopped: bool = False,
    paused: bool = False,
    role: str = "agent-runner",
) -> None:
    conn.execute(
        "INSERT INTO machines (name, role, gateway_url, is_staging, stopped_at, paused_at) "
        "VALUES (%s, ARRAY[%s], 'http://x:1', %s, "
        "        CASE WHEN %s THEN now() END, CASE WHEN %s THEN now() END)",
        (name, role, staging, stopped, paused),
    )
    conn.commit()


class _Probe:
    def __init__(self, answers: dict[str, dict[str, Any] | None]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    async def __call__(self, target_machine: str, **_kw: object) -> dict[str, Any]:
        self.calls.append(target_machine)
        answer = self.answers.get(target_machine)
        if answer is None:
            raise ClusterOpUnreachable("unreachable")
        return answer


def _status(name: str, **extra: object) -> dict[str, Any]:
    return {
        "machine_name": name,
        "serve_gateway": False,
        "serve_agent_runner": True,
        "paused": False,
        "running_sha": "sha-1",
        "agent_host_online": True,
        **extra,
    }


def _snapshot(conn: psycopg.Connection, name: str) -> tuple[Any, ...] | None:
    return conn.execute(
        "SELECT reachable, consecutive_failures, status->>'running_sha', status_at IS NOT NULL "
        "FROM machine_status_snapshot WHERE machine_name = %s",
        (name,),
    ).fetchone()


def _judged(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute("SELECT machine_name FROM machine_probe ORDER BY 1").fetchall()
    return [r[0] for r in rows]


def test_a_probed_machine_is_snapshotted_with_its_status(
    db_conn: psycopg.Connection, pool: ConnectionPool, *, database_gate: ProcessDbGate
) -> None:
    _machine(db_conn, "runner-1")

    asyncio.run(
        run_liveness_pass(
            Database.from_settings(gate=database_gate),
            pool,
            EventBus.from_settings(),
            probe=_Probe({"runner-1": _status("runner-1")}),
            config=LivenessConfig(8.0),
        )
    )

    assert _snapshot(db_conn, "runner-1") == (True, 0, "sha-1", True)


def test_one_failed_probe_keeps_the_last_status_and_counts_the_failure(
    db_conn: psycopg.Connection, pool: ConnectionPool, *, database_gate: ProcessDbGate
) -> None:
    _machine(db_conn, "runner-1")
    asyncio.run(
        run_liveness_pass(
            Database.from_settings(gate=database_gate),
            pool,
            EventBus.from_settings(),
            probe=_Probe({"runner-1": _status("runner-1")}),
            config=LivenessConfig(8.0),
        )
    )

    asyncio.run(
        run_liveness_pass(
            Database.from_settings(gate=database_gate),
            pool,
            EventBus.from_settings(),
            probe=_Probe({"runner-1": None}),
            config=LivenessConfig(8.0),
        )
    )
    assert _snapshot(db_conn, "runner-1") == (False, 1, "sha-1", True)

    asyncio.run(
        run_liveness_pass(
            Database.from_settings(gate=database_gate),
            pool,
            EventBus.from_settings(),
            probe=_Probe({"runner-1": None}),
            config=LivenessConfig(8.0),
        )
    )
    assert _snapshot(db_conn, "runner-1") == (False, 2, "sha-1", True)

    asyncio.run(
        run_liveness_pass(
            Database.from_settings(gate=database_gate),
            pool,
            EventBus.from_settings(),
            probe=_Probe({"runner-1": _status("runner-1")}),
            config=LivenessConfig(8.0),
        )
    )
    assert _snapshot(db_conn, "runner-1") == (True, 0, "sha-1", True)


def test_a_reachable_answer_that_is_not_a_cluster_status_clears_the_status(
    db_conn: psycopg.Connection, pool: ConnectionPool, *, database_gate: ProcessDbGate
) -> None:
    _machine(db_conn, "runner-1")
    asyncio.run(
        run_liveness_pass(
            Database.from_settings(gate=database_gate),
            pool,
            EventBus.from_settings(),
            probe=_Probe({"runner-1": _status("runner-1")}),
            config=LivenessConfig(8.0),
        )
    )

    asyncio.run(
        run_liveness_pass(
            Database.from_settings(gate=database_gate),
            pool,
            EventBus.from_settings(),
            probe=_Probe({"runner-1": {"result": "?"}}),
            config=LivenessConfig(8.0),
        )
    )

    assert _snapshot(db_conn, "runner-1") == (True, 0, None, False)


def test_staging_and_stopped_hosts_are_snapshotted_but_not_judged(
    db_conn: psycopg.Connection, pool: ConnectionPool, *, database_gate: ProcessDbGate
) -> None:
    _machine(db_conn, "target")
    _machine(db_conn, "laptop", staging=True)
    _machine(db_conn, "retired", stopped=True)
    probe = _Probe({"target": _status("target"), "laptop": _status("laptop"), "retired": None})

    asyncio.run(
        run_liveness_pass(
            Database.from_settings(gate=database_gate),
            pool,
            EventBus.from_settings(),
            probe=probe,
            config=LivenessConfig(8.0),
        )
    )

    assert sorted(probe.calls) == ["laptop", "retired", "target"]
    assert _snapshot(db_conn, "laptop") == (True, 0, "sha-1", True)
    assert _snapshot(db_conn, "retired") == (False, 1, None, False)
    assert _judged(db_conn) == ["target"]
    assert db_conn.execute("SELECT count(*) FROM alerts").fetchone() == (0,)


def test_paused_and_non_runner_machines_are_not_probed(
    db_conn: psycopg.Connection, pool: ConnectionPool, *, database_gate: ProcessDbGate
) -> None:
    _machine(db_conn, "target")
    _machine(db_conn, "held", paused=True)
    _machine(db_conn, "station", role="observability-station")
    probe = _Probe({"target": _status("target")})

    asyncio.run(
        run_liveness_pass(
            Database.from_settings(gate=database_gate),
            pool,
            EventBus.from_settings(),
            probe=probe,
            config=LivenessConfig(8.0),
        )
    )

    assert probe.calls == ["target"]
    assert _snapshot(db_conn, "held") is None and _snapshot(db_conn, "station") is None


class _ObservedRound:
    def __init__(self, events: list[str], error: Exception | None = None) -> None:
        self._events = events
        self._error = error

    async def run_round(self) -> None:
        self._events.append("pass")
        if self._error is not None:
            raise self._error


class _ObservedTimers(ManualTimers):
    def __init__(self, events: list[str], stop_after: int) -> None:
        super().__init__()
        self._events = events
        self._stop_after = stop_after

    async def asleep(self, seconds: float) -> None:
        self._events.append("sleep")
        self.sleeps.append(seconds)
        if self._events.count("sleep") == self._stop_after:
            raise asyncio.CancelledError
        self.advance(seconds)


def test_the_first_liveness_pass_runs_at_start_not_after_an_interval() -> None:
    events: list[str] = []
    timers = _ObservedTimers(events, stop_after=1)
    clock = Clock(ClockConfig("UTC", "UTC", False), timers=timers, now=timers.now)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            heartbeat_loops.liveness_loop(
                _ObservedRound(events), LoopProgress("liveness", 60.0, timers=clock), timers=clock
            )
        )
    assert events == ["pass", "sleep"]


def test_a_failing_pass_waits_out_the_interval_before_retrying() -> None:
    events: list[str] = []
    timers = _ObservedTimers(events, stop_after=3)
    clock = Clock(ClockConfig("UTC", "UTC", False), timers=timers, now=timers.now)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            heartbeat_loops.liveness_loop(
                _ObservedRound(events, RuntimeError("probe fan-out failed")),
                LoopProgress("liveness", 60.0, timers=clock),
                timers=clock,
            )
        )
    assert events == ["pass", "sleep", "sleep", "pass", "sleep"]
    assert timers.sleeps == [30.0, 30.0, 30.0]


def test_an_unexpected_probe_error_is_not_counted_as_an_unreachable_host() -> None:
    async def probe(**_kw: object) -> dict[str, Any]:
        raise RuntimeError("bug in the probe")

    with pytest.raises(RuntimeError, match="bug in the probe"):
        asyncio.run(probe_machine("runner-1", probe, config=LivenessConfig(8.0)))
