"""The dead show() page scan: a loop of the page-server service that closes the rows
of agent-owned page servers that died and tells the owner once."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import base.db
from base.agents.tests.units import spawn_agent
from base.clock import SYSTEM_TIMERS
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import ConfigBoot
from base.config.service_read import ConfigAuthority
from base.daemon.loop_health import LoopProgress
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus, event_bus_config_from_settings
from base.lm.catalog import ModelCatalog
from base.native_process.loaded_commit import LoadedCommit
from services.agent_runner.page_server import daemon, dead_pages
from services.agent_runner.page_server.tests.slices import page_server_config

_HOST = "127.0.0.1"


@pytest.fixture
def pool(db_url: str, *, database_gate: ProcessDbGate) -> Iterator[ConnectionPool]:
    p = base.db.pool(max_size=2, gate=database_gate)
    try:
        yield p
    finally:
        p.close()


class _RecordingBus(EventBus):
    def __init__(self, seen: dict[str, list[Any]]) -> None:
        super().__init__(event_bus_config_from_settings())
        self.seen = seen

    async def publish_best_effort(
        self, payload: str, *, channel: str | None = None, context: str = ""
    ) -> int | None:
        del channel, context
        self.seen["events"].append(payload)
        return 1


@pytest.fixture
def published() -> dict[str, list[Any]]:
    """Record the PageClosed frames and the wakes the pass publishes."""
    return {"events": [], "wakes": []}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind((_HOST, 0))
        return int(s.getsockname()[1])


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


async def _round(
    pool: ConnectionPool,
    *,
    database_gate: ProcessDbGate,
    bus: EventBus | None = None,
    alive_fn: Callable[[str, int], bool] = dead_pages.page_server_alive,
) -> None:
    await dead_pages.dead_pages_round(
        pool,
        Database.from_settings(gate=database_gate),
        _HOST,
        _progress(),
        EventBus.from_settings() if bus is None else bus,
        alive_fn=alive_fn,
    )


def test_only_open_show_pages_of_this_host_are_selected(
    redis_url: str,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
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
    published: dict[str, list[Any]],
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
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

    await _round(pool, database_gate=database_gate, bus=_RecordingBus(published))

    assert _closed(db_conn, agent, "dead-one") and _closed(db_conn, agent, "dead-two")
    assert _notices(db_conn, agent) == []
    assert len(published["events"]) == 2
    assert published["wakes"] == []


async def test_a_live_show_page_and_a_dead_serve_page_are_left_alone(
    redis_url: str,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    published: dict[str, list[Any]],
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
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
    probed: list[int] = []

    def alive(_host: str, port: int) -> bool:
        probed.append(port)
        return port == alive_port

    await _round(
        pool,
        database_gate=database_gate,
        bus=_RecordingBus(published),
        alive_fn=alive,
    )

    assert probed == [alive_port]
    assert not _closed(db_conn, agent, "alive") and not _closed(db_conn, agent, "serve-dead")
    assert _notices(db_conn, agent) == [] and published["events"] == []


async def test_the_loop_scans_at_once_then_paces(
    redis_url: str,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    database: Database,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
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
            database,
            _HOST,
            _progress(),
            page_server_config(),
            EventBus.from_settings(),
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


def test_each_loop_gets_its_own_progress_tracker(
    database: Database,
    *,
    machine_identity: MachineIdentity,
    process_config: ConfigBoot,
    tmp_path: Path,
) -> None:
    received: dict[str, LoopProgress] = {}

    async def reconcile(
        _pool: object,
        progress: LoopProgress,
        _config: object,
        _bus: object,
        *,
        local_machine: MachineIdentity,
    ) -> None:
        assert local_machine is machine_identity
        received["reconcile"] = progress

    async def dead(
        _pool: object,
        _db: object,
        _host: str,
        progress: LoopProgress,
        _config: object,
        _bus: object,
    ) -> None:
        received["dead"] = progress

    image = LoadedCommit(Path("/page-server-tests"), None)
    asyncio.run(
        asyncio.wait_for(
            daemon.run(
                process_config=process_config,
                runtime=tmp_path / "run",
                database=lambda: database,
                image=image,
                local_machine=machine_identity,
                loop_runners=(reconcile, dead),
            ),
            timeout=5.0,
        )
    )

    assert sorted(received) == ["dead", "reconcile"]
    assert len({id(p) for p in received.values()}) == 2


@pytest.mark.parametrize("crashing", ["reconcile", "dead"])
def test_a_crashing_loop_cancels_its_sibling_and_ends_the_service(
    crashing: str,
    database: Database,
    *,
    machine_identity: MachineIdentity,
    process_config: ConfigBoot,
    tmp_path: Path,
) -> None:
    cancelled: list[str] = []
    observed_pools: list[ConnectionPool] = []
    pidfile = tmp_path / "run" / "page_server.pid"

    def loop(name: str) -> Callable[..., Any]:
        async def run_loop(pool: ConnectionPool, *_args: object) -> None:
            observed_pools.append(pool)
            assert pidfile.exists()
            if name == crashing:
                await asyncio.sleep(0.01)
                raise RuntimeError(f"{name} crashed")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(name)
                raise

        return run_loop

    async def reconcile_loop(*args: Any, local_machine: MachineIdentity) -> None:
        assert local_machine is machine_identity
        await loop("reconcile")(*args)

    image = LoadedCommit(Path("/page-server-tests"), None)

    with pytest.raises(ExceptionGroup) as raised:
        asyncio.run(
            asyncio.wait_for(
                daemon.run(
                    process_config=process_config,
                    runtime=tmp_path / "run",
                    database=lambda: database,
                    image=image,
                    local_machine=machine_identity,
                    loop_runners=(reconcile_loop, loop("dead")),
                ),
                timeout=5.0,
            )
        )

    assert [str(exc) for exc in raised.value.exceptions] == [f"{crashing} crashed"]
    assert cancelled == [({"reconcile", "dead"} - {crashing}).pop()]
    assert observed_pools and all(p.closed for p in observed_pools)
    assert not pidfile.exists()
