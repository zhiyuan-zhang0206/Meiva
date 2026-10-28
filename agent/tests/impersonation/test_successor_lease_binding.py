# meiva:note
# What a replacement native incarnation does with the lease its predecessor accepted.
# `adopt_for_successor` moves the accepting pair to the admitted incarnation and keeps
# everything else, so the external controller keeps its credential; the move is recorded
# in the lease's own log and emitted as `impersonation_lease_adopted`. A termination is not
# a restart, so a pending terminate still ends the lease at admission.
import asyncio
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from base.agents.impersonation.tests.impersonation_support import recorded_tree
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog


async def _active_lease(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine: MachineIdentityReader,
    catalog: ModelCatalog,
) -> tuple[int, Any, Any]:
    from agent.ownership.hosted import admit_hosted_runtime
    from base.agents import impersonation as leases
    from base.agents.tests.units import spawn_agent
    from base.api_contracts.caller_identity import CallerIdentity

    agent_id = spawn_agent(
        catalog=catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine,
    )
    first = await admit_hosted_runtime(aops_pool, agent_id, machine.name(), uuid4(), db=database)
    assert first is not None
    lease = leases.request(
        database,
        event_bus,
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
        ttl_seconds=3600,
        reason="Handle the next message",
        process_metadata=recorded_tree(),
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
        authority=config_authority,
        local_machine=machine.name(),
        name="Test name",
        executor_name="Test executor_name",
    )
    leases.prepare(
        database,
        event_bus,
        lease["id"],
        agent_id,
        first,
        "Handoff brief",
        local_machine=machine.name(),
    )
    leases.activate(database, event_bus, lease["id"], first)
    return agent_id, first, lease


async def test_successor_admission_carries_an_active_lease(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    from agent.ownership.hosted import admit_hosted_runtime
    from base.agents import impersonation as leases

    agent_id, first, lease = await _active_lease(
        db_conn,
        aops_pool,
        database,
        event_bus,
        config_authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
        catalog=model_catalog,
    )
    successor = await admit_hosted_runtime(
        aops_pool, agent_id, machine_identity.name(), uuid4(), db=database
    )
    assert successor is not None
    assert successor.generation != first.generation
    assert db_conn.execute(
        "SELECT status,rejection_reason,accepted_generation,accepted_owner,ended_at,"
        "event_admission_closed_at FROM agent_impersonations WHERE id=%s",
        (lease["id"],),
    ).fetchone() == ("active", None, successor.generation, successor.owner, None, None)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND source='system:impersonation'",
        (agent_id,),
    ).fetchone() == (0,)
    assert db_conn.execute(
        "SELECT terminal_notice_attempts FROM agent_impersonations WHERE id=%s",
        (lease["id"],),
    ).fetchone() == (0,)
    payloads = [
        payload
        for (payload,) in db_conn.execute(
            "SELECT payload FROM agent_impersonation_entries WHERE lease_id=%s ORDER BY seq",
            (lease["id"],),
        ).fetchall()
    ]
    assert [p for p in payloads if p.get("event") == "lease_adopted"] == [
        {
            "event": "lease_adopted",
            "from": {"generation": str(first.generation), "owner": str(first.owner)},
            "to": {"generation": str(successor.generation), "owner": str(successor.owner)},
            "relay_provider": "codex",
        }
    ]
    assert db_conn.execute(
        "SELECT attributes->>'lease_id',attributes->>'from_generation',"
        "attributes->>'to_generation' FROM audit_events "
        "WHERE event_name='impersonation_lease_adopted' AND agent_id=%s",
        (agent_id,),
    ).fetchall() == [(str(lease["id"]), str(first.generation), str(successor.generation))]
    carried = await asyncio.to_thread(
        leases.native_status, database, event_bus, agent_id, successor
    )
    assert carried is not None and carried["status"] == "active"
    with pytest.raises(leases.ImpersonationError, match="no longer owns"):
        leases.native_status(database, event_bus, agent_id, first)
    db_conn.commit()
    released = leases.release(
        database,
        event_bus,
        lease["id"],
        "Done after the restart",
        local_machine=machine_identity.name(),
    )
    assert released["status"] == "released"
    assert db_conn.execute(
        "SELECT runtime_generation,runtime_owner FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone() == (successor.generation, successor.owner)


# meiva:note
# The move and the entry that records it are one statement, and this is the guard: a recording
# connection counts the writes `adopt_for_successor` makes and reads their shape. Splitting them
# back into an update followed by an insert is the change this pins against, and no database
# failure can pin it — the only constructible one, a log sequence collision, also breaks the
# fallback's own trigger, so both shapes fail identically.
class _RecordedConnection:
    def __init__(self, row: tuple[Any, ...]) -> None:
        self.row = row
        self.statements: list[str] = []

    async def execute(self, query: str, params: Any = None) -> Any:
        self.statements.append(query)
        row = self.row if len(self.statements) == 1 else None

        class Cursor:
            async def fetchone(self) -> tuple[Any, ...] | None:
                return row

        return Cursor()

    def transaction(self) -> Any:
        raise AssertionError("the move is one statement and needs no savepoint")


async def test_the_move_and_its_record_are_one_statement() -> None:
    from base.agents.impersonation.replacement import adopt_for_successor
    from base.native_process.runtime_incarnation import RuntimeIncarnation

    lease_id = str(uuid4())
    from_generation, from_owner = uuid4(), uuid4()
    incarnation = RuntimeIncarnation(7, uuid4(), uuid4())
    conn = _RecordedConnection((lease_id, 3, "active", from_generation, from_owner, "codex"))

    adoption = await adopt_for_successor(conn, 7, incarnation)

    assert adoption is not None
    assert len(conn.statements) == 2
    move = conn.statements[1]
    assert "UPDATE agent_impersonations" in move
    assert "INSERT INTO agent_impersonation_entries" in move
