# meiva:note
# Remote rounds compose three independently safe shell reclamation paths: terminal owners first,
# then expired rows, then stale unexpired rows. These integration tests use the real database and
# runner wire stub to pin that ordering without duplicating expiry/stale-path edge cases.
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.api_contracts.agent_contract import ShellKillMode
from base.clock.tests.fakes import system_clock
from base.daemon.loop_health import LoopProgress
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from ops.rpc_schemas import ShellInfo, ShellKillResult, ShellProbeResult
from ops.tests.rpc_support import rpc_client
from services.upkeep.ttl_reaper.remote import remote_round
from services.upkeep.ttl_reaper.tests.runner_stub import Reply, completed, runner
from services.upkeep.ttl_reaper.tests.slices import reaper_config


# meiva:note
# The production reaper takes a synchronous pool, so the integration boundary
# uses a small real pool rather than an async test convenience wrapper.
@pytest.fixture()
def reaper_pool(db_url: str, *, database_gate: ProcessDbGate) -> Iterator[ConnectionPool]:
    """The reaper's bounded pool for this database-integrated module."""
    import base.db

    pool = base.db.pool(max_size=2, gate=database_gate)
    yield pool
    pool.close()


def _agent(conn: psycopg.Connection, *, status: str = "running") -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, spawner, status, machine) VALUES (%s, 'user', %s, 'macmini')",
        (agent_id, status),
    )
    conn.commit()
    return agent_id


def _shell(
    conn: psycopg.Connection,
    agent_id: int,
    session_id: int,
    *,
    expires_at: datetime,
    created_at: datetime | None = None,
) -> None:
    conn.execute(
        "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at, created_at) "
        "VALUES (%s, %s, %s, %s)",
        (agent_id, session_id, expires_at, created_at or expires_at - timedelta(hours=1)),
    )
    conn.commit()


def _rows(conn: psycopg.Connection, agent_id: int) -> list[int]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT session_id FROM agent_shell_ttls WHERE agent_id = %s ORDER BY session_id",
            (agent_id,),
        ).fetchall()
    ]


def _progress() -> LoopProgress:
    return LoopProgress("test", 60.0, clock=system_clock())


def _killed(*, interrupted: bool) -> dict[str, Any]:
    return ShellKillResult(mode=ShellKillMode.KILLED, interrupted=interrupted).model_dump(
        mode="json"
    )


def _probed(*session_ids: int) -> dict[str, Any]:
    return ShellProbeResult(
        shells=[ShellInfo(id=session_id) for session_id in session_ids]
    ).model_dump(mode="json")


# meiva:note
# A TTL row can remain unexpired after a home-host failure or a direct durable terminal write.
# The regular remote round must dispatch the home-machine close immediately and delete only after
# the remote verdict, rather than wait for expiry or a stale sweep.
async def test_remote_round_closes_an_unexpired_cross_machine_terminal_owner_shell(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = _agent(db_conn, status="terminated")
    _shell(db_conn, agent_id, 9, expires_at=datetime.now(UTC) + timedelta(days=365))

    with runner(db_conn, "macmini", lambda _call: completed(_killed(interrupted=True))) as wire:
        await remote_round(
            reaper_pool,
            Database.from_settings(gate=database_gate),
            event_bus,
            _progress(),
            reaper_config(),
            rpc_client()[0],
        )

    assert [(call["kind"], call["payload"]) for call in wire.calls] == [
        ("shell_kill", {"agent_id": agent_id, "session_id": 9})
    ]
    assert _rows(db_conn, agent_id) == []


async def test_remote_round_kills_the_expired_row_and_sweeps_the_dead_sessions_row(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = _agent(db_conn)
    _shell(db_conn, agent_id, 5, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    _shell(
        db_conn,
        agent_id,
        6,
        expires_at=datetime.now(UTC) + timedelta(days=365),
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )

    def answer(call: dict[str, Any]) -> Reply:
        return completed(_killed(interrupted=False) if call["kind"] == "shell_kill" else _probed())

    with runner(db_conn, "macmini", answer) as wire:
        await remote_round(
            reaper_pool,
            Database.from_settings(gate=database_gate),
            event_bus,
            _progress(),
            reaper_config(),
            rpc_client()[0],
        )

    assert [call["kind"] for call in wire.calls] == ["shell_kill", "shell_probe"]
    assert _rows(db_conn, agent_id) == []


async def test_remote_round_without_its_sweep_leaves_the_rows_alone(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = _agent(db_conn)
    _shell(db_conn, agent_id, 1, expires_at=datetime.now(UTC) + timedelta(days=365))

    with runner(db_conn, "macmini", lambda _call: completed(_probed())) as wire:
        await remote_round(
            reaper_pool,
            Database.from_settings(gate=database_gate),
            event_bus,
            _progress(),
            reaper_config(),
            rpc_client()[0],
            sweep=False,
        )

    assert wire.calls == []
    assert _rows(db_conn, agent_id) == [1]
