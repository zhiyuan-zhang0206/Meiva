import asyncio
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.ownership.hosted import (
    admit_hosted_runtime,
    apply_hosted_lifecycle,
    settle_hosted_runtime,
)
from agent.ownership.tests.ownership_support import admit_owner, agent_row, insert_lifecycle_command
from base.agents.context import MeivaContext
from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.packages.composer_commands import CommandCatalog
from ops import cluster_status
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.tests.host_policy import configured_machine, configured_policy
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
            policy=configured_policy(),
            pool=aops_pool,
            checkpointer=Mock(),
            graph=_graph_blocked_until_released(kind, entered, release),
            machine=configured_machine(name="claim-test"),
            bus=EventBus.from_settings(),
            db=Database.from_settings(gate=database_gate),
            catalog=model_catalog,
            commands=CommandCatalog(skills=list),
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
                        agent=AgentSlices.resolve(
                            default_reader=configured_policy().default_reader
                        ),
                        db=Database.from_settings(gate=database_gate),
                        bus=EventBus.from_settings(),
                        catalog=model_catalog,
                        clock_factory=configured_policy().clock_factory,
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
        graph.ainvoke = AsyncMock(
            return_value={
                "exit_requested": True,
                "restart_requested": False,
                "turn_idle": False,
                "halted": True,
            }
        )
        host = AgentHost(
            policy=configured_policy(),
            pool=aops_pool,
            checkpointer=Mock(),
            graph=graph,
            machine=configured_machine(name="claim-test"),
            bus=EventBus.from_settings(),
            db=Database.from_settings(gate=database_gate),
            catalog=model_catalog,
            commands=CommandCatalog(skills=list),
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
            await apply_hosted_lifecycle(pool, token, **kwargs)
            raise RuntimeError("injected post-commit crash")

        await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)
        with monkeypatch.context() as patch:
            if crash == "after_cache_drop":
                patch.setattr(host, "drop_agent", fail_drop)
            elif crash == "before_observe":
                patch.setattr(psycopg.AsyncConnection, "execute", fail_observe)
            else:
                patch.setattr(
                    "services.agent_runner.agent_host.invocation.apply_hosted_lifecycle",
                    fail_after_commit,
                )
            with pytest.raises(RuntimeError, match="injected"):
                await host._invoke_until_done(
                    agent_id,
                    replace(
                        MeivaContext(
                            ops_pool=aops_pool,
                            agent=AgentSlices.resolve(
                                default_reader=configured_policy().default_reader
                            ),
                            db=Database.from_settings(gate=database_gate),
                            bus=EventBus.from_settings(),
                            catalog=model_catalog,
                            clock_factory=configured_policy().clock_factory,
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
                        agent=AgentSlices.resolve(
                            default_reader=configured_policy().default_reader
                        ),
                        db=Database.from_settings(gate=database_gate),
                        bus=EventBus.from_settings(),
                        catalog=model_catalog,
                        clock_factory=configured_policy().clock_factory,
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
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine=configured_machine(name="claim-test"),
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
        commands=CommandCatalog(skills=list),
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


def _record_kills(
    monkeypatch: pytest.MonkeyPatch, *, fail: bool = False
) -> list[tuple[int, str | None]]:
    calls: list[tuple[int, str | None]] = []

    def _kill(agent_id: int, *, before_session_index: int) -> list[int]:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
            row = conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent_id,)).fetchone()
        calls.append((agent_id, None if row is None else row[0]))
        if fail:
            raise RuntimeError("failed to kill shell session(s) [4]")
        return [4]

    monkeypatch.setattr(cluster_status, "kill_agent_shells", _kill)
    return calls


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
        graph.ainvoke = AsyncMock(
            return_value={
                "exit_requested": True,
                "restart_requested": False,
                "turn_idle": False,
                "halted": True,
            }
        )
        host = AgentHost(
            policy=configured_policy(),
            pool=aops_pool,
            checkpointer=Mock(),
            graph=graph,
            machine=configured_machine(name="claim-test"),
            bus=EventBus.from_settings(),
            db=Database.from_settings(gate=database_gate),
            catalog=model_catalog,
            commands=CommandCatalog(skills=list),
        )
        host._runtimes[agent_id] = Mock(harness_bridge=None)
        assert await host._invoke_until_done(
            agent_id,
            MeivaContext(
                original_incarnation=incarnation,
                hosted_resources=resources,
                ops_pool=aops_pool,
                agent=AgentSlices.resolve(default_reader=configured_policy().default_reader),
                db=Database.from_settings(gate=database_gate),
                bus=EventBus.from_settings(),
                catalog=model_catalog,
                clock_factory=configured_policy().clock_factory,
            ),
        )


@pytest.mark.parametrize("requested", [True, False])
async def test_hosted_terminate_kills_requested_shell_sessions_after_commit(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    requested: bool,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    terminate_command(db_conn, agent_id, kill=requested)
    kills = _record_kills(monkeypatch)
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
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    own = terminate_command(db_conn, agent_id, source="self")
    terminate_command(db_conn, agent_id, kill=True)
    kills = _record_kills(monkeypatch)
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
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    terminate_command(db_conn, agent_id, kill=True)
    kills = _record_kills(monkeypatch, fail=True)
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
