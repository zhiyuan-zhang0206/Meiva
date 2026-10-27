import asyncio
import socket
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.ownership.hosted import admit_hosted_runtime, settle_hosted_runtime
from agent.ownership.tests.ownership_support import admit_owner, agent_row, insert_lifecycle_command
from base.agents.context import MeivaContext
from base.clock import Clock
from base.clock.tests.seeded_rng import seeded_rng
from base.config import settings
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.packages.composer_commands import CommandCatalog
from base.paths import meiva_home, run_dir
from base.sessions.pty import protocol
from base.sessions.pty.paths import service_socket_path
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.lifecycle.shells import apply_lifecycle_command
from services.agent_runner.agent_host.tests.host_policy import (
    configured_machine,
    configured_policy,
    handoff_timestamps,
)
from services.agent_runner.agent_host.tests.runtime.hosted_resources import hosted_scope


def _graph_blocked_until_released(
    kind: str, entered: asyncio.Event, release: asyncio.Event
) -> Mock:

    async def graph_return(*args: object, **kwargs: object) -> dict[str, bool]:
        entered.set()
        await release.wait()
        return {
            "exit_requested": kind == "terminate",
            "restart_requested": kind == "restart",
            "turn_idle": False,
            "halted": True,
        }

    graph = Mock()
    graph.aget_state = AsyncMock(return_value=SimpleNamespace(values={}))
    graph.ainvoke = AsyncMock(side_effect=graph_return)
    return graph


def _assert_command_unapplied_while_continuation_runs(
    db_conn: psycopg.Connection,
    host: AgentHost,
    old: RuntimeIncarnation,
    *,
    agent_id: int,
    inbound: int,
) -> None:
    assert db_conn.execute(
        "SELECT runtime_generation,runtime_owner,status FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone() == (old.generation, old.owner, "running")
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (inbound,)
    ).fetchone() == ("claimed",)
    assert agent_id in host._runtimes


async def _assert_restart_released_the_runtime(
    db_conn: psycopg.Connection,
    pool: AsyncConnectionPool,
    old: RuntimeIncarnation,
    database: Database,
    event_bus: EventBus,
    *,
    agent_id: int,
) -> None:
    assert not await settle_hosted_runtime(pool, old, bus=event_bus)
    new = await admit_hosted_runtime(pool, agent_id, "claim-test", uuid4(), db=database)
    assert new is not None and new.generation != old.generation


@pytest.mark.parametrize("kind", ["restart", "terminate"])
async def test_hosted_applies_only_after_continuation_returns(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    kind: str,
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    async with hosted_scope() as resources:
        agent_id = agent_row(db_conn)
        old = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
        inbound = insert_lifecycle_command(db_conn, agent_id, kind)
        entered, release = asyncio.Event(), asyncio.Event()
        host = AgentHost(
            runtime_owner=uuid4(),
            handoff_stamping=handoff_timestamps(),
            home=meiva_home(),
            policy=configured_policy(),
            pool=aops_pool,
            checkpointer=Mock(),
            graph=_graph_blocked_until_released(kind, entered, release),
            machine=configured_machine(name="claim-test"),
            bus=EventBus.from_settings(rng=seeded_rng()),
            db=Database.from_settings(gate=database_gate),
            catalog=model_catalog,
            commands=CommandCatalog.system(skills=list),
        )
        host._runtimes[agent_id] = Mock(harness_bridge=None)
        assert [
            row.id for row in await claim_inbound_batch(aops_pool, agent_id, incarnation=old)
        ] == [inbound]
        assert db_conn.execute(
            "SELECT status FROM inbound_messages WHERE id=%s", (inbound,)
        ).fetchone() == ("claimed",)
        task = asyncio.create_task(
            host._invoke_until_done(
                agent_id,
                replace(
                    MeivaContext(
                        ops_pool=aops_pool,
                        agent=AgentSlices.resolve(default_reader=defaults()),
                        db=Database.from_settings(gate=database_gate),
                        bus=EventBus.from_settings(rng=seeded_rng()),
                        catalog=model_catalog,
                        clock_factory=Clock.from_settings,
                    ),
                    original_incarnation=old,
                    hosted_resources=resources,
                ),
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        try:
            _assert_command_unapplied_while_continuation_runs(
                db_conn, host, old, agent_id=agent_id, inbound=inbound
            )
        finally:
            release.set()
            await asyncio.wait_for(task, 3)
        assert agent_id not in host._runtimes
        assert db_conn.execute(
            "SELECT status FROM inbound_messages WHERE id=%s", (inbound,)
        ).fetchone() == ("done",)
        if kind == "restart":
            await _assert_restart_released_the_runtime(
                db_conn, aops_pool, old, database, event_bus, agent_id=agent_id
            )


@pytest.mark.parametrize("crash", ["after_cache_drop", "before_observe", "after_commit"])
async def test_hosted_terminate_crash_leaves_no_half_applied_command(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    crash: str,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    async with hosted_scope() as resources:
        agent_id = agent_row(db_conn)
        owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
        inbound = insert_lifecycle_command(db_conn, agent_id, "terminate")
        graph = Mock()
        graph.aget_state = AsyncMock(return_value=SimpleNamespace(values={}))
        graph.ainvoke = AsyncMock(
            return_value={
                "exit_requested": True,
                "restart_requested": False,
                "turn_idle": False,
                "halted": True,
            }
        )
        host = AgentHost(
            runtime_owner=uuid4(),
            handoff_stamping=handoff_timestamps(),
            home=meiva_home(),
            policy=configured_policy(),
            pool=aops_pool,
            checkpointer=Mock(),
            graph=graph,
            machine=configured_machine(name="claim-test"),
            bus=EventBus.from_settings(rng=seeded_rng()),
            db=Database.from_settings(gate=database_gate),
            catalog=model_catalog,
            commands=CommandCatalog.system(skills=list),
        )
        host._runtimes[agent_id] = Mock(harness_bridge=None)
        original_execute = psycopg.AsyncConnection.execute
        original_drop = host.drop_agent

        async def fail_observe(
            conn: psycopg.AsyncConnection, query: Any, *args: Any, **kwargs: Any
        ) -> Any:
            if "UPDATE agents_meta SET status='terminated'" in str(query):
                raise RuntimeError("injected apply crash")
            return await original_execute(conn, query, *args, **kwargs)

        def fail_drop(target: int) -> None:
            original_drop(target)
            raise RuntimeError("injected cache crash")

        async def fail_after_commit(
            pool: AsyncConnectionPool, token: RuntimeIncarnation, **kwargs: Any
        ) -> str | None:
            await apply_lifecycle_command(pool, token, **kwargs)
            raise RuntimeError("injected post-commit crash")

        await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)
        with monkeypatch.context() as patch:
            if crash == "after_cache_drop":
                patch.setattr(host, "drop_agent", fail_drop)
            elif crash == "before_observe":
                patch.setattr(psycopg.AsyncConnection, "execute", fail_observe)
            else:
                patch.setattr(
                    "services.agent_runner.agent_host.invocation.apply_lifecycle_command",
                    fail_after_commit,
                )
            with pytest.raises(RuntimeError, match="injected"):
                await host._invoke_until_done(
                    agent_id,
                    replace(
                        MeivaContext(
                            ops_pool=aops_pool,
                            agent=AgentSlices.resolve(default_reader=defaults()),
                            db=Database.from_settings(gate=database_gate),
                            bus=EventBus.from_settings(rng=seeded_rng()),
                            catalog=model_catalog,
                            clock_factory=Clock.from_settings,
                        ),
                        original_incarnation=owner,
                        hosted_resources=resources,
                    ),
                )
        state = db_conn.execute(
            "SELECT i.status,m.status FROM inbound_messages i JOIN agents_meta m "
            "ON m.id=i.agent_id WHERE i.id=%s",
            (inbound,),
        ).fetchone()
        # meiva:note
        # The command closes together with the termination or not at all.
        assert state == (
            ("done", "terminated") if crash == "after_commit" else ("claimed", "running")
        )
        db_conn.commit()
        if crash != "after_commit":
            assert await host._invoke_until_done(
                agent_id,
                replace(
                    MeivaContext(
                        ops_pool=aops_pool,
                        agent=AgentSlices.resolve(default_reader=defaults()),
                        db=Database.from_settings(gate=database_gate),
                        bus=EventBus.from_settings(rng=seeded_rng()),
                        catalog=model_catalog,
                        clock_factory=Clock.from_settings,
                    ),
                    original_incarnation=owner,
                    hosted_resources=resources,
                ),
            )
        assert db_conn.execute(
            "SELECT i.status,m.status FROM inbound_messages i JOIN agents_meta m "
            "ON m.id=i.agent_id WHERE i.id=%s",
            (inbound,),
        ).fetchone() == ("done", "terminated")


async def test_pending_wake_scan_finds_a_claimed_command_without_pending_rows(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    inbound = insert_lifecycle_command(db_conn, agent_id, "restart")
    assert [
        row.id for row in await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)
    ] == [inbound]
    assert await settle_hosted_runtime(
        aops_pool,
        owner,
        bus=event_bus,
    )
    host = AgentHost(
        runtime_owner=uuid4(),
        handoff_stamping=handoff_timestamps(),
        home=meiva_home(),
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine=configured_machine(name="claim-test"),
        bus=EventBus.from_settings(rng=seeded_rng()),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
        commands=CommandCatalog.system(skills=list),
    )
    assert agent_id in await host.pending_inbound_wakes()
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND status='pending'", (agent_id,)
    ).fetchone() == (0,)


def terminate_command(
    conn: psycopg.Connection, agent_id: int, *, source: str = "user", kill: bool = False
) -> int:
    row = conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source,payload) "
        "VALUES (%s,'','terminate',%s,%s) RETURNING id",
        (agent_id, source, Jsonb({"kill_all_shell_sessions": True}) if kill else None),
    ).fetchone()
    conn.commit()
    assert row is not None
    return row[0]


@contextmanager
def _record_kills(agent_id: int, *, fail: bool = False) -> Generator[list[tuple[int, str | None]]]:
    calls: list[tuple[int, str | None]] = []
    sock_path = service_socket_path(run_dir(meiva_home()))
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(4)
    server.settimeout(0.05)
    stop = threading.Event()

    def _serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with conn:
                line = protocol.read_line(conn, limit=65536)
                if line is None:
                    continue
                req = protocol.decode_object(line)
                with psycopg.connect(settings.data_plane.db_url, autocommit=True) as db:
                    row = db.execute(
                        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
                    ).fetchone()
                calls.append((agent_id, None if row is None else row[0]))
                reply: dict[str, Any]
                if fail:
                    reply = {
                        "id": req["id"],
                        "ok": False,
                        "code": 1,
                        "error": "failed to kill shell session(s) [4]",
                    }
                else:
                    reply = {"id": req["id"], "ok": True, "data": {"sessions": []}}
                conn.sendall(protocol.encode(reply))

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        yield calls
    finally:
        stop.set()
        server.close()
        thread.join(timeout=2)
        sock_path.unlink(missing_ok=True)


async def _run_terminating_turn(
    aops_pool: AsyncConnectionPool,
    agent_id: int,
    *,
    incarnation: RuntimeIncarnation,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    async with hosted_scope() as resources:
        graph = Mock()
        graph.aget_state = AsyncMock(return_value=SimpleNamespace(values={}))
        graph.ainvoke = AsyncMock(
            return_value={
                "exit_requested": True,
                "restart_requested": False,
                "turn_idle": False,
                "halted": True,
            }
        )
        host = AgentHost(
            runtime_owner=uuid4(),
            handoff_stamping=handoff_timestamps(),
            home=meiva_home(),
            policy=configured_policy(),
            pool=aops_pool,
            checkpointer=Mock(),
            graph=graph,
            machine=configured_machine(name="claim-test"),
            bus=EventBus.from_settings(rng=seeded_rng()),
            db=Database.from_settings(gate=database_gate),
            catalog=model_catalog,
            commands=CommandCatalog.system(skills=list),
        )
        host._runtimes[agent_id] = Mock(harness_bridge=None)
        assert await host._invoke_until_done(
            agent_id,
            MeivaContext(
                original_incarnation=incarnation,
                hosted_resources=resources,
                ops_pool=aops_pool,
                agent=AgentSlices.resolve(default_reader=defaults()),
                db=Database.from_settings(gate=database_gate),
                bus=EventBus.from_settings(rng=seeded_rng()),
                catalog=model_catalog,
                clock_factory=Clock.from_settings,
            ),
        )


@pytest.mark.parametrize("requested", [True, False])
async def test_hosted_terminate_kills_requested_shell_sessions_after_commit(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    requested: bool,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    terminate_command(db_conn, agent_id, kill=requested)
    with _record_kills(agent_id) as kills:
        await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)
        await _run_terminating_turn(
            aops_pool,
            agent_id,
            incarnation=owner,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
    assert kills == ([(agent_id, "terminated")] if requested else [])
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("terminated",)


async def test_hosted_self_terminate_honors_a_queued_kill_request(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    own = terminate_command(db_conn, agent_id, source="self")
    terminate_command(db_conn, agent_id, kill=True)
    with _record_kills(agent_id) as kills:
        assert [
            row.id for row in await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)
        ] == [own]
        await _run_terminating_turn(
            aops_pool,
            agent_id,
            incarnation=owner,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
    assert kills == [(agent_id, "terminated")]


async def test_hosted_failed_kill_still_applies_the_termination(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    loguru_records: list[dict[str, Any]],
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    terminate_command(db_conn, agent_id, kill=True)
    with _record_kills(agent_id, fail=True) as kills:
        await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)
        await _run_terminating_turn(
            aops_pool,
            agent_id,
            incarnation=owner,
            model_catalog=model_catalog,
            database_gate=database_gate,
        )
    assert kills == [(agent_id, "terminated")]
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("terminated",)
    assert any(
        record["level"].name == "ERROR" and "could not kill" in record["message"]
        for record in loguru_records
    )
