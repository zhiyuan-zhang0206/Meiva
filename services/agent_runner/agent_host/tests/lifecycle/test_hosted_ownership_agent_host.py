import asyncio
from unittest.mock import Mock
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

import services.agent_runner.agent_host.host as agent_host_host
from agent.ownership import hosted
from agent.ownership.hosted import admit_hosted_runtime, settle_hosted_runtime
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.packages.composer_commands import CommandCatalog
from services.agent_runner.agent_host import runtime
from services.agent_runner.agent_host.tests.host_policy import configured_machine, configured_policy


def _agent(conn: psycopg.Connection) -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, status, machine) VALUES (%s, 'idling', 'host-test') "
        "ON CONFLICT (id) DO UPDATE SET status = 'idling', machine = 'host-test'",
        (agent_id,),
    )
    conn.commit()
    return agent_id


async def test_cancel_during_live_announce_settles_the_committed_admission(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    from base.native_process.turn_identity import HostedTurnResources
    from services.agent_runner.agent_host.host import AgentHost

    agent_id = _agent(db_conn)
    announce_entered = asyncio.Event()
    announce_release = asyncio.Event()
    publish_calls = 0

    async def half_open_publish(_bus: object, published_agent_id: int) -> None:
        nonlocal publish_calls
        assert published_agent_id == agent_id
        publish_calls += 1
        if publish_calls == 1:
            announce_entered.set()
            await announce_release.wait()

    monkeypatch.setattr(hosted, "publish_agent_updated", half_open_publish)
    monkeypatch.setattr(
        agent_host_host,
        "publish_agent_updated",
        half_open_publish,
        raising=False,
    )

    def allow_model_config(
        *, model: str | None = None, catalog: ModelCatalog, llm_override: str | None
    ) -> None:
        assert model is not None
        assert catalog is model_catalog
        del llm_override

    monkeypatch.setattr(runtime, "validate_model_config", allow_model_config)

    host = AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        control_pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine=configured_machine(name="host-test"),
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
        commands=CommandCatalog(skills=list),
    )
    task = asyncio.create_task(host._run_turn(agent_id, resources=HostedTurnResources()))
    await asyncio.wait_for(announce_entered.wait(), timeout=2.0)
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("running",)

    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=1.0)
    cancellation_settled = bool(done)
    if not cancellation_settled:
        announce_release.set()
        await asyncio.gather(task, return_exceptions=True)

    assert cancellation_settled, "announcement cancellation did not finish settlement"
    assert task.cancelled()
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("idling",)
    assert publish_calls == 2, "settlement must announce after restoring durable status"
    assert agent_id not in host._in_flight


class _StopAtRuntimeError(Exception):
    pass


@pytest.mark.parametrize("status", ["running", "idling"])
async def test_host_takes_over_a_row_last_owned_by_another_instance(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    database: Database,
    event_bus: EventBus,
    *,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    from services.agent_runner.agent_host.host import AgentHost

    agent_id = _agent(db_conn)
    original = await admit_hosted_runtime(aops_pool, agent_id, "host-test", uuid4(), db=database)
    assert original is not None
    if status == "idling":
        assert await settle_hosted_runtime(
            aops_pool,
            original,
            bus=event_bus,
        )
    host = AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=Mock(),
        graph=Mock(),
        machine=configured_machine(name="host-test"),
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
        commands=CommandCatalog(skills=list),
    )

    admitted: list[RuntimeIncarnation] = []

    async def stop_at_runtime(
        _agent_id: int,
        _fingerprint: str,
        _slices: object,
        *,
        incarnation: RuntimeIncarnation,
        **_: object,
    ) -> None:
        admitted.append(incarnation)
        raise _StopAtRuntimeError

    monkeypatch.setattr(host, "_runtime_for", stop_at_runtime)
    with pytest.raises(_StopAtRuntimeError):
        await host.run_turn(agent_id)

    [taken] = admitted
    assert taken.owner == host._owner != original.owner
    assert taken.generation != original.generation
