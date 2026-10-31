import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.impersonation import settle_checkpoint
from agent.impersonation_handoff import flush_checkpoint
from agent.ownership.hosted import admit_hosted_runtime
from agent.tests.impersonation.handoff_inputs import (
    assert_resume_note_delivery_contract,
    assert_unacknowledged_input_preserved,
    controller_relay_alive,
    deliver_peers_and_ack_first,
    end_external_session,
    handoff_timestamps,
    inbound_wakes,
    local_exec_process,
    prepare_graph,
    resume_native,
    stop_takeover_with_exhausted_ack,
    stop_takeover_with_lost_relay,
    takeover_host,
)
from agent.tests.impersonation.handoff_inputs import handoff_clients as handoff_clients
from base.agents import impersonation as leases
from base.agents.context.clients import ClientSet
from base.agents.impersonation import delivery
from base.agents.inbox.insert import insert_inbound_message
from base.agents.observation.relay_supervision import RelaySupervision
from base.cluster.machine import MachineIdentityReader
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus


@pytest.mark.parametrize("cause", ["relay_death", "ack_exhaustion"])
async def test_transport_fault_keeps_native_parked_until_actual_lease_end(
    cause: str,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    tmp_path: Path,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    handoff_clients: ClientSet,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    graph, saver, ctx, config, reset, owner, requested, model_calls = await prepare_graph(
        db_conn,
        aops_pool,
        exec_process=local_exec_process(),
        config_authority=config_authority,
        clients=handoff_clients,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    host = takeover_host(tmp_path, machine_identity)
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None),
    )
    await flush_checkpoint(saver, owner.agent_id)
    controller_relay_alive(db_conn, requested["id"])
    assert await settle_checkpoint(
        graph,
        database,
        event_bus,
        owner.agent_id,
        ctx.relays,
        incarnation=owner,
        timestamps=handoff_timestamps(),
        host=host,
    )
    assert not model_calls
    if cause == "relay_death":
        await stop_takeover_with_lost_relay(database, event_bus, owner, ctx.relays, home=tmp_path)
    else:
        stop_takeover_with_exhausted_ack(db_conn, requested, owner)
    died = leases.get(
        database,
        event_bus,
        requested["id"],
        local_machine=machine_identity.name(),
    )
    assert died["status"] == "active"
    assert died["rejection_reason"] is None
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None),
    )
    assert not model_calls
    leases.release(
        database,
        event_bus,
        requested["id"],
        "Explicit end after preserved delivery fault",
        local_machine=machine_identity.name(),
    )
    with inbound_wakes(event_bus, owner.agent_id) as wakes:
        await resume_native(
            graph, saver, ctx, config, reset, owner, model_calls, wakes, database, event_bus, host
        )
    assert "Explicit end after preserved delivery fault" in model_calls[0].messages[-1].content
    if cause == "ack_exhaustion":
        assert_unacknowledged_input_preserved(host.workspace(owner.agent_id))


async def test_successor_ends_the_takeover_and_resumes_preserved_pending_input(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    tmp_path: Path,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    handoff_clients: ClientSet,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    graph, saver, ctx, config, reset, owner, requested, model_calls = await prepare_graph(
        db_conn,
        aops_pool,
        exec_process=local_exec_process(),
        config_authority=config_authority,
        clients=handoff_clients,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    host = takeover_host(tmp_path, machine_identity)
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None),
    )
    await flush_checkpoint(saver, owner.agent_id)
    controller_relay_alive(db_conn, requested["id"])
    assert await settle_checkpoint(
        graph,
        database,
        event_bus,
        owner.agent_id,
        ctx.relays,
        incarnation=owner,
        timestamps=handoff_timestamps(),
        host=host,
    )
    pending = deliver_peers_and_ack_first(
        db_conn, database, event_bus, requested, owner.agent_id, machine_identity=machine_identity
    )
    successor = await admit_hosted_runtime(
        aops_pool,
        owner.agent_id,
        machine_identity.name(),
        uuid4(),
        db=database,
    )
    assert successor is not None and successor.generation != owner.generation
    with pytest.raises(leases.ImpersonationError, match="no longer owns"):
        leases.native_status(database, event_bus, owner.agent_id, owner)
    assert db_conn.execute(
        "SELECT status,rejection_reason,accepted_generation,accepted_owner,ended_at "
        "FROM agent_impersonations WHERE id=%s",
        (requested["id"],),
    ).fetchone() == ("active", None, successor.generation, successor.owner, None)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (pending,)
    ).fetchone() == ("pending",)
    db_conn.commit()
    replacement_ctx = replace(ctx, relays=RelaySupervision())
    assert not model_calls
    assert await settle_checkpoint(
        graph,
        database,
        event_bus,
        owner.agent_id,
        replacement_ctx.relays,
        incarnation=successor,
        timestamps=handoff_timestamps(),
        host=host,
    )
    assert not model_calls
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (pending,)
    ).fetchone() == ("pending",)
    assert db_conn.execute(
        "SELECT runtime_generation,runtime_owner FROM agents_meta WHERE id=%s",
        (owner.agent_id,),
    ).fetchone() == (successor.generation, successor.owner)


async def test_late_ack_after_delivery_budget_exhaustion_keeps_native_parked(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    tmp_path: Path,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    handoff_clients: ClientSet,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    graph, saver, ctx, config, reset, owner, requested, model_calls = await prepare_graph(
        db_conn,
        aops_pool,
        exec_process=local_exec_process(),
        config_authority=config_authority,
        clients=handoff_clients,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    host = takeover_host(tmp_path, machine_identity)
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None),
    )
    await flush_checkpoint(saver, owner.agent_id)
    controller_relay_alive(db_conn, requested["id"])
    assert await settle_checkpoint(
        graph,
        database,
        event_bus,
        owner.agent_id,
        ctx.relays,
        incarnation=owner,
        timestamps=handoff_timestamps(),
        host=host,
    )
    pending = deliver_peers_and_ack_first(
        db_conn, database, event_bus, requested, owner.agent_id, machine_identity=machine_identity
    )
    token = str(uuid4())
    minted = leases.provision_relay(database, requested["id"], owner, token)
    assert minted is not None
    active = minted.lease
    for _ in range(active["max_delivery_attempts"]):
        assert pending in {
            row["id"]
            for row in leases.relay_inbox(
                database, requested["id"], token, local_machine=machine_identity.name()
            )
        }
        assert delivery.reserve_delivery(
            database,
            event_bus,
            requested["id"],
            token,
            [pending],
            local_machine=machine_identity.name(),
        ) == {pending}
        db_conn.execute(
            "UPDATE agent_impersonation_messages SET last_delivery_at="
            "clock_timestamp()-%s*interval '1 second' WHERE lease_id=%s AND inbound_id=%s",
            (active["ack_window_seconds"] + 1, requested["id"], pending),
        )
        db_conn.commit()
    assert not delivery.reserve_delivery(
        database,
        event_bus,
        requested["id"],
        token,
        [pending],
        local_machine=machine_identity.name(),
    )
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None),
    )
    assert not model_calls
    leases.ack(
        database,
        event_bus,
        requested["id"],
        [pending],
        local_machine=machine_identity.name(),
    )
    assert (
        leases.relay_inbox(database, requested["id"], token, local_machine=machine_identity.name())
        == []
    )
    assert db_conn.execute(
        "SELECT i.status,m.delivery_attempts,m.acknowledged_at IS NOT NULL "
        "FROM inbound_messages i JOIN agent_impersonation_messages m ON m.inbound_id=i.id "
        "WHERE i.id=%s AND m.lease_id=%s",
        (pending, requested["id"]),
    ).fetchone() == ("done", active["max_delivery_attempts"], True)
    db_conn.commit()
    lease = leases.require_active(
        database,
        requested["id"],
        local_machine=machine_identity.name(),
    )
    assert lease["expires_at"] == active["expires_at"]
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None),
    )
    assert not model_calls


async def test_end_note_resumes_an_empty_queue(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    tmp_path: Path,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    handoff_clients: ClientSet,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    graph, saver, ctx, config, reset, owner, requested, model_calls = await prepare_graph(
        db_conn,
        aops_pool,
        exec_process=local_exec_process(),
        config_authority=config_authority,
        clients=handoff_clients,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    host = takeover_host(tmp_path, machine_identity)
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None),
    )
    assert not model_calls
    await flush_checkpoint(saver, owner.agent_id)
    controller_relay_alive(db_conn, requested["id"])
    assert await settle_checkpoint(
        graph,
        database,
        event_bus,
        owner.agent_id,
        ctx.relays,
        incarnation=owner,
        timestamps=handoff_timestamps(),
        host=host,
    )
    leases.release(
        database,
        event_bus,
        requested["id"],
        "External work complete",
        local_machine=machine_identity.name(),
    )
    with inbound_wakes(event_bus, owner.agent_id) as wakes:
        assert not await settle_checkpoint(
            graph,
            database,
            event_bus,
            owner.agent_id,
            ctx.relays,
            incarnation=owner,
            timestamps=handoff_timestamps(),
            host=host,
        )
        assert wakes.drain() == ["impersonation"]
    assert not model_calls

    resumed = await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None),
    )
    await flush_checkpoint(saver, owner.agent_id)
    assert len(model_calls) == 1
    note = model_calls[0].messages[-1]
    assert note.id == f"impersonation-handoff:{owner.agent_id}:0"
    assert note.additional_kwargs["meiva_note_tag"] == "impersonation"
    assert_resume_note_delivery_contract(note.content)
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None),
    )
    assert len(model_calls) == 1
    assert resumed["impersonation_handoff_id"] == f"{owner.agent_id}:0"


async def test_acknowledged_but_unfinished_input_reaches_the_resumed_native(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    tmp_path: Path,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    handoff_clients: ClientSet,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    graph, saver, ctx, config, reset, owner, requested, model_calls = await prepare_graph(
        db_conn,
        aops_pool,
        exec_process=local_exec_process(),
        config_authority=config_authority,
        clients=handoff_clients,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )
    host = takeover_host(tmp_path, machine_identity)
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None),
    )
    await flush_checkpoint(saver, owner.agent_id)
    controller_relay_alive(db_conn, requested["id"])
    assert await settle_checkpoint(
        graph,
        database,
        event_bus,
        owner.agent_id,
        ctx.relays,
        incarnation=owner,
        timestamps=handoff_timestamps(),
        host=host,
    )
    inbound_id = insert_inbound_message(
        db_conn,
        owner.agent_id,
        "Rebuild the report; resume from the failing case",
        source="user",
        bus=event_bus,
        database=database,
    )
    db_conn.commit()
    leases.inbox(
        database,
        requested["id"],
        local_machine=machine_identity.name(),
    )
    leases.ack(
        database,
        event_bus,
        requested["id"],
        [inbound_id],
        local_machine=machine_identity.name(),
    )
    end_external_session(
        db_conn, database, event_bus, requested, "expire", machine_identity=machine_identity
    )
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None),
    )
    await flush_checkpoint(saver, owner.agent_id)
    assert not await settle_checkpoint(
        graph,
        database,
        event_bus,
        owner.agent_id,
        ctx.relays,
        incarnation=owner,
        timestamps=handoff_timestamps(),
        host=host,
    )
    await graph.ainvoke(
        reset,
        config,
        context=replace(ctx, original_incarnation=owner, hosted_resources=None),
    )
    await flush_checkpoint(saver, owner.agent_id)
    assert len(model_calls) == 1
    note = model_calls[0].messages[-1]
    assert "Review the summary and incoming requests" in note.content
    assert "Continue any requests whose completion is not established" in note.content
    document = json.loads((host.workspace(owner.agent_id) / "impersonation" / "0.json").read_text())
    message = next(m for m in document["messages"] if m["payload"]["content"].startswith("Rebuild"))
    assert message["acknowledged"] is True
