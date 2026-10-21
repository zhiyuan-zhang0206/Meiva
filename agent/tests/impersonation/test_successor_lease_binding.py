from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.tests.impersonation.test_impersonation import gate_ctx as gate_ctx
from agent.tests.impersonation.test_impersonation import incarnation as incarnation
from agent.tests.impersonation.test_impersonation import relays as relays
from base.agents.impersonation.tests.impersonation_support import recorded_tree
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog


async def test_successor_admission_ends_an_active_lease_without_restoring_the_previous_owner(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    from agent.ownership.hosted import admit_hosted_runtime
    from base.agents import impersonation as leases
    from base.agents.tests.units import spawn_agent
    from base.api_contracts.caller_identity import CallerIdentity

    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    first = await admit_hosted_runtime(
        aops_pool,
        agent_id,
        machine_identity.name(),
        uuid4(),
        db=database,
    )
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
        local_machine=machine_identity.name(),
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
        local_machine=machine_identity.name(),
    )
    leases.activate(database, event_bus, lease["id"], first)
    successor = await admit_hosted_runtime(
        aops_pool,
        agent_id,
        machine_identity.name(),
        uuid4(),
        db=database,
    )
    assert successor is not None
    assert successor.generation != first.generation
    assert db_conn.execute(
        "SELECT status,rejection_reason,handoff_applied_at IS NULL "
        "FROM agent_impersonations WHERE id=%s",
        (lease["id"],),
    ).fetchone() == ("expired", "aborted: native runtime restarted", True)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND source='system:impersonation' "
        "AND content LIKE %s",
        (agent_id, "%native runtime restarted%"),
    ).fetchone() == (0,)
    with pytest.raises(leases.ImpersonationError):
        leases.release(
            database,
            event_bus,
            lease["id"],
            "Done after the restart",
            local_machine=machine_identity.name(),
        )
    db_conn.commit()
    assert db_conn.execute(
        "SELECT runtime_generation,runtime_owner FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone() == (successor.generation, successor.owner)
