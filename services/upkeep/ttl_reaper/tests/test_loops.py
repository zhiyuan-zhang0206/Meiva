from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import base.db
from base.config import ConfigBoot, settings
from base.daemon.loop_health import LivenessGroup, LoopProgress
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.native_process.loaded_commit import LoadedCommit
from services.upkeep.ttl_reaper import cadence, daemon, remote, shells, sweep


@pytest.fixture
def pool(db_url: str, *, database_gate: ProcessDbGate) -> Iterator[ConnectionPool]:
    p = base.db.pool(max_size=4, gate=database_gate)
    try:
        yield p
    finally:
        p.close()


def _progress() -> LoopProgress:
    return LoopProgress("test", 60.0)


def _backdate(db: psycopg.Connection, kind: str, seconds: float) -> None:
    db.execute(
        "UPDATE maintenance_state SET last_run_at = clock_timestamp() - make_interval(secs => %s) "
        "WHERE kind = %s",
        (seconds, kind),
    )
    db.commit()


def _park_or_crash(*, crashing: str, cancelled: list[str]) -> tuple[daemon.LoopFn, daemon.LoopFn]:
    def stand_in(name: str) -> daemon.LoopFn:
        async def loop(
            _pool: ConnectionPool,
            _db: Database,
            _bus: EventBus,
            _progress: LoopProgress,
        ) -> None:
            if name == crashing:
                await asyncio.sleep(0.01)
                raise RuntimeError(f"{name} loop crashed")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(name)
                raise

        return loop

    return stand_in("sweep"), stand_in("remote")


@pytest.mark.parametrize("crashing", ["sweep", "remote"])
async def test_a_crashing_loop_cancels_its_sibling_and_ends_the_service(
    pool: ConnectionPool,
    crashing: str,
    *,
    database_gate: ProcessDbGate,
) -> None:
    cancelled: list[str] = []
    sweep_loop_fn, remote_loop_fn = _park_or_crash(crashing=crashing, cancelled=cancelled)

    with pytest.raises(ExceptionGroup) as raised:
        await daemon.run_loops(
            pool,
            Database.from_settings(gate=database_gate),
            EventBus.from_settings(),
            LivenessGroup(),
            sweep_loop_fn=sweep_loop_fn,
            remote_loop_fn=remote_loop_fn,
        )

    assert [str(exc) for exc in raised.value.exceptions] == [f"{crashing} loop crashed"]
    assert cancelled == [({"sweep", "remote"} - {crashing}).pop()]


async def test_each_loop_reports_its_own_progress(
    pool: ConnectionPool, *, database_gate: ProcessDbGate
) -> None:
    liveness = LivenessGroup()
    sweep_loop_fn, remote_loop_fn = _park_or_crash(crashing="sweep", cancelled=[])

    with pytest.raises(ExceptionGroup):
        await daemon.run_loops(
            pool,
            Database.from_settings(gate=database_gate),
            EventBus.from_settings(),
            liveness,
            sweep_loop_fn=sweep_loop_fn,
            remote_loop_fn=remote_loop_fn,
        )

    assert set(liveness.snapshot()) == {"sweep", "remote"}


async def test_a_crash_leaves_run_after_releasing_its_resources(
    database: Database,
    *,
    process_config: ConfigBoot,
    tmp_path: Path,
) -> None:
    observed_pools: list[ConnectionPool] = []
    pidfile = tmp_path / "run" / "ttl_reaper.pid"

    async def crashing_loops(
        pool: ConnectionPool, _db: Database, _bus: EventBus, _liveness: LivenessGroup
    ) -> None:
        observed_pools.append(pool)
        assert pidfile.exists()
        raise ExceptionGroup("loops", [RuntimeError("sweep loop crashed")])

    with pytest.raises(ExceptionGroup):
        await daemon.run(
            process_config=process_config,
            runtime=tmp_path / "run",
            database=lambda: database,
            image=LoadedCommit(Path(), None),
            run_loops_fn=crashing_loops,
        )

    assert len(observed_pools) == 1
    assert observed_pools[0].closed
    assert not pidfile.exists()


def test_a_phase_is_claimed_once_per_interval(
    db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    assert cadence.claim_due(pool, cadence.FIRE_LOG_PRUNE, 3600.0) is True
    assert cadence.claim_due(pool, cadence.FIRE_LOG_PRUNE, 3600.0) is False

    _backdate(db_conn, cadence.FIRE_LOG_PRUNE, 3601.0)
    assert cadence.claim_due(pool, cadence.FIRE_LOG_PRUNE, 3600.0) is True
    assert cadence.claim_due(pool, cadence.FIRE_LOG_PRUNE, 3600.0) is False


def test_a_claim_survives_a_restart(
    db_conn: psycopg.Connection, pool: ConnectionPool, *, database_gate: ProcessDbGate
) -> None:
    assert cadence.claim_due(pool, cadence.FIRE_LOG_PRUNE, 86400.0) is True
    restarted = base.db.pool(max_size=2, gate=database_gate)
    try:
        assert cadence.claim_due(restarted, cadence.FIRE_LOG_PRUNE, 86400.0) is False
    finally:
        restarted.close()
    row = db_conn.execute("SELECT count(*) FROM maintenance_state").fetchone()
    assert row is not None and row[0] == 1


def _count_slow_phases() -> tuple[dict[str, int], Any]:
    calls = {"prune": 0}

    def prune(_pool: ConnectionPool) -> int:
        calls["prune"] += 1
        return 0

    return calls, prune


async def test_slow_phases_run_once_per_cadence_not_once_per_round(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    database: Database,
) -> None:
    calls, prune = _count_slow_phases()

    await sweep.sweep_round(
        pool,
        database,
        EventBus.from_settings(),
        _progress(),
        cleanup_interval_s=86400.0,
        prune_fire_log_fn=prune,
    )
    await sweep.sweep_round(
        pool,
        database,
        EventBus.from_settings(),
        _progress(),
        cleanup_interval_s=86400.0,
        prune_fire_log_fn=prune,
    )
    assert calls == {"prune": 1}

    _backdate(db_conn, cadence.FIRE_LOG_PRUNE, 90000.0)
    await sweep.sweep_round(
        pool,
        database,
        EventBus.from_settings(),
        _progress(),
        cleanup_interval_s=86400.0,
        prune_fire_log_fn=prune,
    )
    assert calls == {"prune": 2}


async def test_a_restarted_sweep_does_not_rerun_phases_it_already_ran(
    pool: ConnectionPool, database: Database
) -> None:
    calls, prune = _count_slow_phases()
    await sweep.sweep_round(
        pool, database, EventBus.from_settings(), _progress(), prune_fire_log_fn=prune
    )
    assert calls == {"prune": 1}

    await sweep.sweep_round(
        pool, database, EventBus.from_settings(), _progress(), prune_fire_log_fn=prune
    )
    assert calls == {"prune": 1}


async def test_the_remote_round_reaps_shells(
    pool: ConnectionPool, *, database_gate: ProcessDbGate
) -> None:
    calls: list[str] = []

    async def reap(
        _pool: ConnectionPool, _db: Database, _bus: EventBus, _progress: LoopProgress
    ) -> list[tuple[int, int]]:
        calls.append("shells")
        return []

    await remote.remote_round(
        pool,
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        _progress(),
        reap_shells_fn=reap,
    )

    assert calls == ["shells"]


async def test_machines_are_reclaimed_concurrently_and_one_machines_rows_in_order(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
) -> None:
    slow_aid = create_agent(db_conn)
    fast_aid = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status, machine) "
            "VALUES (%s, 'user', 'running', 'slow'), (%s, 'user', 'running', 'fast')",
            (slow_aid, fast_aid),
        )
        for aid, session_id in ((slow_aid, 1), (fast_aid, 2), (slow_aid, 3), (fast_aid, 4)):
            cur.execute(
                "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at, created_at) "
                "VALUES (%s, %s, now() - interval '1 minute', now() - interval '1 hour')",
                (aid, session_id),
            )
    db_conn.commit()

    release = asyncio.Event()
    started: list[tuple[str, int]] = []
    finished: list[tuple[str, int]] = []

    async def dispatch(
        _db: object, machine: str, _kind: str, payload: dict[str, Any], **_kw: object
    ) -> dict[str, Any]:
        session_id = int(payload["session_id"])
        started.append((machine, session_id))
        if machine == "slow":
            await release.wait()
        finished.append((machine, session_id))
        return {"mode": "absent", "interrupted": False}

    task = asyncio.create_task(
        shells.reap_expired_shells(
            pool,
            Database.from_settings(gate=database_gate),
            event_bus,
            _progress(),
            dispatch_fn=dispatch,
        )
    )
    for _ in range(100):
        if [m for m, _ in finished].count("fast") == 2:
            break
        await asyncio.sleep(0.01)
    assert finished == [("fast", 2), ("fast", 4)]
    assert started == [("slow", 1), ("fast", 2), ("fast", 4)]

    release.set()
    reaped = await task
    assert sorted(sn for _aid, sn in reaped) == [1, 2, 3, 4]
    assert [s for m, s in started if m == "slow"] == [1, 3]


async def test_a_dispatch_past_its_deadline_defers_the_row(*, database_gate: ProcessDbGate) -> None:

    async def hang(*_a: object, **_kw: object) -> dict[str, Any]:
        await asyncio.Event().wait()
        return {}

    assert (
        await shells.dispatch_shell_kill(
            Database.from_settings(gate=database_gate),
            "macmini",
            1,
            2,
            dispatch_fn=hang,
            deadline_s=0.05,
        )
        is None
    )


def test_the_dispatch_deadline_covers_the_clients_full_retry_budget() -> None:
    attempts = max(settings.gateway.cluster_rpc_max_retries, 0) + 1
    assert shells.dispatch_deadline_s() >= attempts * 5.0
    assert shells.dispatch_deadline_s() < attempts * settings.gateway.cluster_rpc_timeout_seconds
