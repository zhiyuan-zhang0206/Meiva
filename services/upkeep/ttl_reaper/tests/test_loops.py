from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import ConnectionPool

import base.db
from base.clock import SYSTEM_TIMERS
from base.config import ConfigBoot, settings
from base.daemon.endpoints import ServiceEndpoints
from base.daemon.loop_health import LivenessGroup
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.native_process.loaded_commit import LoadedCommit
from services.upkeep.ttl_reaper import cadence, daemon, shells
from services.upkeep.ttl_reaper.tests.slices import reaper_config


@pytest.fixture
def pool(db_url: str, *, database_gate: ProcessDbGate) -> Iterator[ConnectionPool]:
    p = base.db.pool(max_size=4, gate=database_gate)
    try:
        yield p
    finally:
        p.close()


def _backdate(db: psycopg.Connection, kind: str, seconds: float) -> None:
    db.execute(
        "UPDATE maintenance_state SET last_run_at = clock_timestamp() - make_interval(secs => %s) "
        "WHERE kind = %s",
        (seconds, kind),
    )
    db.commit()


# meiva:note
# A table that is not there, produced by the throwaway Postgres itself: while the block is open the
# table is renamed away, so every statement that names it raises `UndefinedTable`.
@contextmanager
def _table_missing(conn: psycopg.Connection, table: str) -> Generator[None]:
    moved = sql.Identifier(f"{table}_moved")
    conn.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(sql.Identifier(table), moved))
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        conn.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(moved, sql.Identifier(table)))
        conn.commit()


# meiva:note
# The sweep dies on the web-session statement and the remote loop on the shell-row select: each
# loop's own first round meets the missing table while its sibling has nothing to do and waits
# out its poll interval.
@pytest.mark.parametrize(
    ("crashing", "table"), [("sweep", "web_sessions"), ("remote", "agent_shell_ttls")]
)
async def test_a_crashing_loop_cancels_its_sibling_and_ends_the_service(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    crashing: str,
    table: str,
    *,
    database_gate: ProcessDbGate,
) -> None:
    liveness = LivenessGroup(timers=SYSTEM_TIMERS)
    before = asyncio.all_tasks()

    with _table_missing(db_conn, table), pytest.raises(ExceptionGroup) as raised:
        await asyncio.wait_for(
            daemon.run_loops(
                pool,
                Database.from_settings(gate=database_gate),
                EventBus.from_settings(),
                liveness,
                reaper_config(),
            ),
            timeout=30.0,
        )

    assert [type(exc) for exc in raised.value.exceptions] == [psycopg.errors.UndefinedTable]
    assert set(liveness.snapshot()) == {"sweep", "remote"}, crashing
    assert asyncio.all_tasks() == before, "the sibling loop was left running"


def _our_backends(db_conn: psycopg.Connection) -> int:
    db_conn.rollback()
    row = db_conn.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE 'meiva:%%'"
    ).fetchone()
    assert row is not None
    return int(row[0])


async def test_a_crash_leaves_run_after_releasing_its_resources(
    db_conn: psycopg.Connection,
    database: Database,
    *,
    process_config: ConfigBoot,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "run"
    pidfile = runtime / "ttl_reaper.pid"
    port = ServiceEndpoints.from_boot(process_config, runtime=runtime).of("ttl_reaper").health_port

    with _table_missing(db_conn, "agent_shell_ttls"), pytest.raises(ExceptionGroup):
        await asyncio.wait_for(
            daemon.run(
                process_config=process_config,
                runtime=runtime,
                database=lambda: database,
                image=LoadedCommit(Path(), None),
            ),
            timeout=30.0,
        )

    assert not pidfile.exists()
    with pytest.raises(ConnectionRefusedError), socket.create_connection(("127.0.0.1", port), 1.0):
        pass
    deadline = time.monotonic() + 10.0
    while _our_backends(db_conn) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert _our_backends(db_conn) == 0, "run left its pool open"


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


def test_the_configuration_is_read_once_from_the_boot_owner(process_config: ConfigBoot) -> None:
    config = daemon.ttl_reaper_config(process_config)

    assert config.poll_interval_s == process_config.view.daemon.ttl_reaper_poll_interval_seconds
    assert (
        config.fire_log_cleanup_interval_s
        == process_config.view.daemon.schedule_fire_log_cleanup_interval_seconds
    )
    assert (
        config.fire_log_retention_days
        == process_config.view.daemon.schedule_fire_log_retention_days
    )
    assert config.shell_kill_deadline_s == shells.dispatch_deadline_s()


def test_the_dispatch_deadline_covers_the_clients_full_retry_budget() -> None:
    attempts = max(settings.gateway.cluster_rpc_max_retries, 0) + 1
    assert shells.dispatch_deadline_s() >= attempts * 5.0
    assert shells.dispatch_deadline_s() < attempts * settings.gateway.cluster_rpc_timeout_seconds
