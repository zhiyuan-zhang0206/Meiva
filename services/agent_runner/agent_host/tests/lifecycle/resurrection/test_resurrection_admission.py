from collections.abc import Awaitable, Iterator
from typing import Any, Protocol
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.ownership.hosted import admit_hosted_runtime, apply_hosted_lifecycle
from base.agents.birth.spawn import create_agent_row
from base.agents.inbox.insert import insert_inbound_message
from base.agents.messages.inbound import InboundKind
from base.api_contracts.agent_contract import ResurrectError
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.native_process.runtime_incarnation import RuntimeIncarnation
from ops import lifecycle
from ops.agents import wake
from ops.cluster import rpc as cluster_rpc
from ops.cluster.rpc import ClusterOpFailed
from services.agent_runner.agent_host.tests.lifecycle.resurrection.resurrection_support import (
    force,
    status,
    terminated,
)


@pytest.fixture(autouse=True)
def wakes(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[int, str]]]:
    captured: list[tuple[int, str]] = []

    def _record(_db: object, _bus: object, agent_id: int, payload: str) -> None:
        captured.append((agent_id, payload))

    monkeypatch.setattr(wake, "publish_inbound_wake", _record)
    yield captured


def _identity(db: psycopg.Connection, aid: int) -> tuple[Any, Any]:
    row = db.execute(
        "SELECT runtime_generation,runtime_owner FROM agents_meta WHERE id=%s", (aid,)
    ).fetchone()
    db.commit()
    assert row is not None
    return row[0], row[1]


@pytest.mark.parametrize("guarded", [False, True])
async def test_terminated_row_resurrects_unowned_and_admits_a_fresh_generation(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    wakes: list[tuple[int, str]],
    guarded: bool,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    aid = terminated(
        db_conn,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    trigger = (
        insert_inbound_message(db_conn, aid, "continue", "user", bus=event_bus, database=database)
        if guarded
        else None
    )
    db_conn.commit()

    wake.resurrect_agent(
        database,
        event_bus,
        aid,
        resurrected_by="system" if guarded else "user",
        trigger_inbound_id=trigger,
        trigger_inbound_kind=InboundKind.CHAT if guarded else None,
    )

    assert status(db_conn, aid) == "idling"
    assert _identity(db_conn, aid) == (None, None)
    assert wakes == [(aid, "0")]
    incarnation = await admit_hosted_runtime(
        aops_pool, aid, machine_identity.name(), uuid4(), db=database
    )
    assert incarnation is not None
    assert _identity(db_conn, aid) == (incarnation.generation, incarnation.owner)


@pytest.mark.parametrize("terminate_after_trigger", [False, True])
def test_trigger_must_be_newer_than_every_terminate_command(
    db_conn: psycopg.Connection,
    terminate_after_trigger: bool,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    aid = terminated(
        db_conn,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    if terminate_after_trigger:
        trigger = insert_inbound_message(
            db_conn, aid, "continue", "user", bus=event_bus, database=database
        )
        insert_inbound_message(
            db_conn, aid, "", "user", kind="terminate", bus=event_bus, database=database
        )
    else:
        insert_inbound_message(
            db_conn, aid, "", "user", kind="terminate", bus=event_bus, database=database
        )
        trigger = insert_inbound_message(
            db_conn, aid, "continue", "user", bus=event_bus, database=database
        )
    db_conn.commit()

    with db_conn.cursor() as cur:
        if terminate_after_trigger:
            with pytest.raises(ResurrectError, match="0 rows"):
                wake.transition_terminated_to_unclaimed_idling(
                    cur,
                    aid,
                    trigger_inbound_id=trigger,
                    trigger_inbound_kind=InboundKind.CHAT,
                )
            db_conn.rollback()
            assert status(db_conn, aid) == "terminated"
        else:
            wake.transition_terminated_to_unclaimed_idling(
                cur,
                aid,
                trigger_inbound_id=trigger,
                trigger_inbound_kind=InboundKind.CHAT,
            )
            db_conn.commit()
            assert status(db_conn, aid) == "idling"


async def test_unknown_auto_resurrect_failure_propagates(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    aid = terminated(
        db_conn,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    trigger = insert_inbound_message(
        db_conn, aid, "hello", "user", bus=event_bus, database=database
    )
    db_conn.commit()

    async def _failed(*_a: object, **_kw: object) -> dict[str, Any]:
        raise ClusterOpFailed({"error": "launch failed on the home machine"})

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _failed)
    with pytest.raises(ClusterOpFailed):
        await lifecycle.resurrect_if_terminated(
            Database.from_settings(gate=database_gate),
            event_bus,
            aid,
            trigger_inbound_id=trigger,
            trigger_inbound_kind=InboundKind.CHAT,
            local_machine=machine_identity.name(),
        )
    assert not any(r["extra"].get("event") == "auto_resurrect_failed" for r in loguru_records)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (trigger,)
    ).fetchone() == ("pending",)


class _Arrange(Protocol):
    def __call__(
        self,
        conn: psycopg.Connection,
        pool: AsyncConnectionPool,
        /,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> Awaitable[int]: ...


def _unowned_idle(db: psycopg.Connection, aid: int) -> bool:
    row = db.execute(
        "SELECT status,runtime_generation,runtime_owner FROM agents_meta WHERE id=%s",
        (aid,),
    ).fetchone()
    db.commit()
    return row == ("idling", None, None)


async def admit_hosted_incarnation(
    pool: AsyncConnectionPool,
    aid: int,
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentity,
) -> RuntimeIncarnation:
    owner = await admit_hosted_runtime(
        pool,
        aid,
        machine_identity.name(),
        uuid4(),
        db=Database.from_settings(gate=database_gate),
    )
    assert owner is not None
    return owner


async def _spawned(
    db: psycopg.Connection,
    pool: AsyncConnectionPool,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> int:
    aid, _, _, _ = create_agent_row(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        spawner="user",
        machine=machine_identity.name(),
        authority=config_authority,
        catalog=model_catalog,
    )
    return aid


async def _resurrected(
    db: psycopg.Connection,
    pool: AsyncConnectionPool,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> int:
    aid = await _spawned(
        db,
        pool,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    await admit_hosted_incarnation(
        pool, aid, database_gate=database_gate, machine_identity=machine_identity
    )
    db.execute(
        "UPDATE agents_meta SET status='terminated',termination_source='user' WHERE id=%s",
        (aid,),
    )
    db.commit()
    wake.resurrect_agent(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        aid,
        resurrected_by="user",
    )
    return aid


async def restarted(
    db: psycopg.Connection,
    pool: AsyncConnectionPool,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> int:
    aid = await _spawned(
        db,
        pool,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    owner = await admit_hosted_incarnation(
        pool, aid, database_gate=database_gate, machine_identity=machine_identity
    )
    insert_inbound_message(
        db,
        aid,
        "",
        "user",
        kind="restart",
        bus=EventBus.from_settings(),
        database=Database.from_settings(gate=database_gate),
    )
    db.commit()
    await claim_inbound_batch(pool, aid, incarnation=owner)
    assert (
        await apply_hosted_lifecycle(
            pool,
            owner,
            bus=EventBus.from_settings(),
            resume_turn=False,
        )
        == "restart"
    )
    return aid


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("arrange", [_spawned, _resurrected, restarted])
async def test_a_row_this_runtime_left_and_ended_unowned_resurrects(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    wakes: list[tuple[int, str]],
    arrange: _Arrange,
    guarded: bool,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    aid = await arrange(
        db_conn,
        aops_pool,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    assert _unowned_idle(db_conn, aid)
    command = force(aid)
    assert command > 0
    trigger = (
        insert_inbound_message(db_conn, aid, "continue", "user", bus=event_bus, database=database)
        if guarded
        else None
    )
    db_conn.commit()
    wakes.clear()

    wake.resurrect_agent(
        database,
        event_bus,
        aid,
        resurrected_by="system" if guarded else "user",
        trigger_inbound_id=trigger,
        trigger_inbound_kind=InboundKind.CHAT if guarded else None,
    )

    assert _unowned_idle(db_conn, aid)
    assert wakes == [(aid, "0")]
    assert await admit_hosted_incarnation(
        aops_pool, aid, database_gate=database_gate, machine_identity=machine_identity
    )
