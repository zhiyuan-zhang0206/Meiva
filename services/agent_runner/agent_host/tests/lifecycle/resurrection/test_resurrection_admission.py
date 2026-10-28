from collections.abc import Awaitable, Iterator
from typing import Any, Protocol
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.ownership.hosted import AppliedLifecycle, admit_hosted_runtime, apply_hosted_lifecycle
from base.agents.birth.spawn import create_agent_row
from base.agents.inbox.insert import insert_inbound_message
from base.agents.messages.inbound import InboundKind
from base.api_contracts.agent_contract import ResurrectError
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.cluster import redis_channel_prefix
from base.cluster.machine import MachineIdentityReader
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.net.tests.local_http import serve_asgi
from base.lm.catalog import ModelCatalog
from base.native_process.runtime_incarnation import RuntimeIncarnation
from ops import lifecycle
from ops.agents import wake
from ops.cluster.rpc import ClusterOpFailed, RpcClient
from ops.tests.rpc_support import rpc_client
from services.agent_runner.agent_host.tests.lifecycle.resurrection.resurrection_support import (
    force,
    status,
    terminated,
)


class _WakeCapture:
    def __init__(self, pubsub: Any) -> None:
        self._pubsub = pubsub
        self._items: list[tuple[int, str]] = []

    def _drain(self) -> None:
        while True:
            raw: dict[str, Any] | None = self._pubsub.get_message(timeout=0.05)
            if raw is None:
                break
            if raw.get("type") != "pmessage":
                continue
            channel = raw["channel"]
            channel_str = channel.decode() if isinstance(channel, bytes) else str(channel)
            data = raw["data"]
            payload = data.decode() if isinstance(data, bytes) else str(data)
            self._items.append((int(channel_str.rpartition(":")[2]), payload))

    def clear(self) -> None:
        self._drain()
        self._items.clear()

    def drained(self) -> list[tuple[int, str]]:
        self._drain()
        return list(self._items)


@pytest.fixture(autouse=True)
def wakes(event_bus: EventBus) -> Iterator[_WakeCapture]:
    redis_client: Any = event_bus.sync_redis()
    pubsub = redis_client.pubsub()
    pubsub.psubscribe(f"{redis_channel_prefix(event_bus.channel)}:inbound:*")
    assert pubsub.get_message(timeout=1.0) is not None
    try:
        yield _WakeCapture(pubsub)
    finally:
        pubsub.close()
        redis_client.close()


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
    wakes: _WakeCapture,
    guarded: bool,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
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
    wakes.clear()

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
    assert wakes.drained() == [(aid, "0")]
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
    machine_identity: MachineIdentityReader,
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
    loguru_records: list[dict[str, Any]],
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
    rpc: RpcClient,
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

    async def failed_ops(scope: Any, receive: Any, send: Any) -> None:
        del scope
        await receive()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"status":"failed","result":{"error":"launch failed on the home machine"}}',
            }
        )

    with serve_asgi(failed_ops) as ops_url:
        db_conn.execute(
            "INSERT INTO machines (name, gateway_url) VALUES (%s, %s) "
            "ON CONFLICT (name) DO UPDATE SET gateway_url = EXCLUDED.gateway_url",
            (machine_identity.name(), ops_url),
        )
        db_conn.commit()
        with pytest.raises(ClusterOpFailed):
            await lifecycle.resurrect_if_terminated(
                Database.from_settings(gate=database_gate),
                event_bus,
                aid,
                trigger_inbound_id=trigger,
                trigger_inbound_kind=InboundKind.CHAT,
                local_machine=machine_identity.name(),
                rpc=rpc,
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
        machine_identity: MachineIdentityReader,
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
    machine_identity: MachineIdentityReader,
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
    machine_identity: MachineIdentityReader,
) -> int:
    aid, _, _, _ = create_agent_row(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
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
    machine_identity: MachineIdentityReader,
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
        EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
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
    machine_identity: MachineIdentityReader,
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
        bus=EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
        database=Database.from_settings(gate=database_gate),
    )
    db.commit()
    await claim_inbound_batch(pool, aid, incarnation=owner)
    assert await apply_hosted_lifecycle(pool, owner, resume_turn=False) == AppliedLifecycle(
        kind="restart", agent_id=aid, shell_cutoff=None
    )
    return aid


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("arrange", [_spawned, _resurrected, restarted])
async def test_a_row_this_runtime_left_and_ended_unowned_resurrects(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    wakes: _WakeCapture,
    arrange: _Arrange,
    guarded: bool,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
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
    assert wakes.drained() == [(aid, "0")]
    assert await admit_hosted_incarnation(
        aops_pool, aid, database_gate=database_gate, machine_identity=machine_identity
    )


# meiva:note
# The cluster client handed to what dispatches: the test credential and retry waits on a manual
# clock, so a retried dispatch costs no time.
@pytest.fixture
def rpc() -> RpcClient:
    return rpc_client()[0]
