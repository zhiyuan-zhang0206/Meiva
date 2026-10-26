"""The dead show() page scan: a loop of the page-server service that closes the rows
of agent-owned page servers that died and tells the owner once."""

from __future__ import annotations

import asyncio
import contextlib
import json
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import base.db
from base.agents.tests.units import spawn_agent
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import ConfigBoot
from base.config.service_read import ConfigAuthority
from base.daemon.loop_health import LoopProgress
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.tests.live_capture import recorded_events
from base.lm.catalog import ModelCatalog
from base.native_process.loaded_commit import LoadedCommit
from services.agent_runner.page_server import daemon, dead_pages, resources
from services.agent_runner.page_server.tests.page_pty import (
    free_port as _free_port,
)
from services.agent_runner.page_server.tests.page_pty import (
    refusing_show_page_close,
    running_page_server,
)
from services.agent_runner.page_server.tests.slices import page_server_config

_HOST = "127.0.0.1"


@pytest.fixture
def pool(db_url: str, *, database_gate: ProcessDbGate) -> Iterator[ConnectionPool]:
    p = base.db.pool(max_size=2, gate=database_gate)
    try:
        yield p
    finally:
        p.close()


def _page(
    conn: psycopg.Connection,
    agent_id: int,
    name: str,
    port: int,
    *,
    serve_dir: str | None = None,
    host: str = _HOST,
) -> None:
    conn.execute(
        "INSERT INTO agent_pages (agent_id, name, port, host, serve_dir) "
        "VALUES (%s, %s, %s, %s, %s)",
        (agent_id, name, port, host, serve_dir),
    )
    conn.commit()


def _closed(conn: psycopg.Connection, agent_id: int, name: str) -> bool:
    row = conn.execute(
        "SELECT closed_at IS NOT NULL FROM agent_pages WHERE agent_id = %s AND name = %s",
        (agent_id, name),
    ).fetchone()
    assert row is not None
    return bool(row[0])


def _notices(conn: psycopg.Connection, agent_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT content FROM inbound_messages WHERE agent_id = %s AND source = 'system'",
        (agent_id,),
    ).fetchall()
    return [r[0] for r in rows]


def _progress() -> LoopProgress:
    return LoopProgress("dead_show_pages", 600.0, timers=SYSTEM_TIMERS)


async def _round(pool: ConnectionPool, *, bus: EventBus | None = None) -> None:
    await dead_pages.dead_pages_round(
        pool,
        _HOST,
        _progress(),
        EventBus.from_settings(rng=seeded_rng()) if bus is None else bus,
    )


def test_only_open_show_pages_of_this_host_are_selected(
    redis_url: str,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    _page(db_conn, agent, "show-open", 18101)
    _page(db_conn, agent, "serve-open", 18102, serve_dir="/data/site")
    _page(db_conn, agent, "elsewhere", 18103, host="10.9.9.9")
    _page(db_conn, agent, "show-closed", 18104)
    db_conn.execute(
        "UPDATE agent_pages SET closed_at = now() WHERE agent_id = %s AND name = 'show-closed'",
        (agent,),
    )
    db_conn.commit()

    assert [p.name for p in dead_pages.open_show_pages(pool, _HOST)] == ["show-open"]


async def test_a_dead_show_page_is_closed_and_its_owner_told_once(
    redis_url: str,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    event_bus: EventBus,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    _page(db_conn, agent, "dead-one", _free_port())
    _page(db_conn, agent, "dead-two", _free_port())

    with recorded_events(event_bus) as events:
        await _round(pool, bus=event_bus)

    assert _closed(db_conn, agent, "dead-one") and _closed(db_conn, agent, "dead-two")
    assert _notices(db_conn, agent) == []
    assert [
        e for e in events if e.get("role") == "page_closed" and e.get("agent_id") == agent
    ] != []
    assert (
        len([e for e in events if e.get("role") == "page_closed" and e.get("agent_id") == agent])
        == 2
    )


async def test_a_live_show_page_and_a_dead_serve_page_are_left_alone(
    redis_url: str,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    event_bus: EventBus,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    """A live show() server is kept; a serve() page is the daemon's own to relaunch,
    so a dead one is neither probed nor closed here."""
    agent = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    alive_port = _free_port()
    _page(db_conn, agent, "alive", alive_port)
    _page(db_conn, agent, "serve-dead", _free_port(), serve_dir="/data/site")

    with (
        running_page_server(tmp_path, alive_port, "show-token"),
        recorded_events(event_bus) as events,
    ):
        await _round(pool, bus=event_bus)

    assert not _closed(db_conn, agent, "alive") and not _closed(db_conn, agent, "serve-dead")
    assert _notices(db_conn, agent) == []
    assert [
        e for e in events if e.get("role") == "page_closed" and e.get("agent_id") == agent
    ] == []


async def test_the_loop_scans_at_once_then_paces(
    redis_url: str,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    _page(db_conn, agent, "dead-at-start", _free_port())
    task = asyncio.create_task(
        dead_pages.dead_pages_loop(
            pool,
            _HOST,
            _progress(),
            page_server_config(),
            EventBus.from_settings(rng=seeded_rng()),
        )
    )
    try:
        for _ in range(200):
            if _closed(db_conn, agent, "dead-at-start"):
                break
            await asyncio.sleep(0.01)
        _page(db_conn, agent, "dead-later", _free_port())
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert _closed(db_conn, agent, "dead-at-start")
    assert not _closed(db_conn, agent, "dead-later")


# --- the service owns both loops ---------------------------------------------


def _healthz(port: int) -> dict[str, Any]:
    with urllib.request.urlopen(f"http://{_HOST}:{port}/healthz", timeout=5) as response:
        return json.loads(response.read())


async def _serve(
    database: Database, config: ConfigBoot, runtime: Path, machine: MachineIdentityReader
) -> None:
    await daemon.run(
        process_config=config,
        runtime=runtime,
        database=lambda: database,
        image=LoadedCommit(Path("/page-server-tests"), None),
        local_machine=machine,
    )


async def _loops_reported(port: int) -> list[str]:
    for _ in range(500):
        with contextlib.suppress(OSError):
            return sorted((await asyncio.to_thread(_healthz, port))["loops"])
        await asyncio.sleep(0.02)
    raise AssertionError("the page-server health endpoint never answered")


async def test_each_loop_gets_its_own_progress_tracker(
    database: Database,
    *,
    machine_identity: MachineIdentityReader,
    process_config: ConfigBoot,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "run"
    port = resources.endpoint(process_config, runtime=runtime).health_port
    service = asyncio.create_task(_serve(database, process_config, runtime, machine_identity))
    try:
        loops = await _loops_reported(port)
    finally:
        service.cancel()
        await asyncio.gather(service, return_exceptions=True)

    assert loops == ["dead_show_pages", "reconcile"]
    assert not (runtime / "page_server.pid").exists()


# meiva:note
# The reconcile pass reads `session_name`, which the dead-show-page scan never does, so a
# schema that lost the column crashes only the reconcile loop.
async def test_a_crashing_reconcile_loop_cancels_its_sibling_and_ends_the_service(
    database: Database,
    db_conn: psycopg.Connection,
    *,
    machine_identity: MachineIdentityReader,
    process_config: ConfigBoot,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "run"
    db_conn.execute("ALTER TABLE agent_pages RENAME COLUMN session_name TO session_name_hidden")
    db_conn.commit()
    try:
        with pytest.raises(ExceptionGroup) as raised:
            await asyncio.wait_for(_serve(database, process_config, runtime, machine_identity), 30)
    finally:
        db_conn.rollback()
        db_conn.execute("ALTER TABLE agent_pages RENAME COLUMN session_name_hidden TO session_name")
        db_conn.commit()

    assert [type(exc).__name__ for exc in raised.value.exceptions] == ["UndefinedColumn"]
    assert not (runtime / "page_server.pid").exists()


@pytest.mark.usefixtures("page_server_identity")
async def test_a_crashing_dead_page_loop_cancels_its_sibling_and_ends_the_service(
    database: Database,
    db_conn: psycopg.Connection,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
    process_config: ConfigBoot,
    redis_url: str,
    tmp_path: Path,
) -> None:
    agent = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    _page(db_conn, agent, "dead-and-unclosable", _free_port())
    runtime = tmp_path / "run"

    with refusing_show_page_close(db_conn), pytest.raises(ExceptionGroup) as raised:
        await asyncio.wait_for(_serve(database, process_config, runtime, machine_identity), 30)

    assert [type(exc).__name__ for exc in raised.value.exceptions] == ["RaiseException"]
    assert not (runtime / "page_server.pid").exists()
