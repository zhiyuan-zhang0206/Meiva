import asyncio
import io
import random
import sys
from dataclasses import replace
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import psycopg
import pytest
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.graph.claim.node import claim_node
from agent.impersonation import process_host, settle_checkpoint
from agent.ownership.hosted import admit_hosted_runtime, apply_hosted_lifecycle
from agent.state import BaseAgentState
from base.agents import impersonation as leases
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.agents.impersonation.maintenance import remind_expiring_impersonations
from base.agents.impersonation.tests.impersonation_support import recorded_tree
from base.agents.messages.timestamp_policy import TimestampSource
from base.api_contracts.caller_identity import CallerIdentity
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import defaults
from base.db import Database, create_agent, pool
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.redis_listener import RedisInboundListener
from base.host.env.agent_slices import AgentSlices
from base.native_process.runtime_incarnation import RuntimeIncarnation
from cli.commands.agents import impersonation_relay as relay
from cli.commands.agents.impersonation_adapters import ClaudeStdout
from cli.commands.agents.relay.steps import RelayTuning
from ops.agents.wake import resurrect_agent
from ops.lifecycle.termination import enqueue_termination_inbounds, force_terminate_transaction


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws.
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


def test_reminder_commands_are_bare_meiva(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    local_machine = machine_identity.name()
    agent_id = create_agent(db_conn)
    owner = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner) "
        "VALUES(%s,'idling',%s,%s,%s)",
        (agent_id, local_machine, owner.generation, owner.owner),
    )
    db_conn.commit()
    lease = leases.request(
        database,
        event_bus,
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
        ttl_seconds=300,
        reason="Handle the next message",
        process_metadata={**recorded_tree(), "invoked_python": "/preview source/.venv/bin/python"},
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
        authority=config_authority,
        local_machine=local_machine,
        name="Test name",
        executor_name="Test executor_name",
    )
    leases.prepare(
        database,
        event_bus,
        lease["id"],
        agent_id,
        owner,
        "Handoff brief",
        local_machine=local_machine,
    )
    leases.activate(database, event_bus, lease["id"], owner)
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()+interval '4 minutes' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    with pool(max_size=2, gate=database_gate) as reaper_pool:
        assert remind_expiring_impersonations(reaper_pool, database, event_bus) == 1
    reminder = db_conn.execute(
        "SELECT content FROM inbound_messages WHERE agent_id=%s AND kind='reminder'",
        (agent_id,),
    ).fetchone()
    assert reminder is not None
    content: str = reminder[0]
    assert f"\nmeiva impersonate renew 0 --agent {agent_id} --ttl 300\n" in content
    assert f"\nmeiva impersonate release 0 --agent {agent_id} --summary ..." in content
    assert "-m cli" not in content
    assert ".venv" not in content
    assert sys.executable not in content


@pytest.mark.parametrize("bad", [7, "", "  ", ["python"]])
def test_request_rejects_a_malformed_invoked_python(
    db_conn: psycopg.Connection,
    bad: object,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentityReader,
) -> None:
    local_machine = machine_identity.name()
    agent_id = create_agent(db_conn)
    owner = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner) "
        "VALUES(%s,'idling',%s,%s,%s)",
        (agent_id, local_machine, owner.generation, owner.owner),
    )
    db_conn.commit()
    with pytest.raises(ValueError, match="invoked_python"):
        leases.request(
            database,
            event_bus,
            agent_id,
            caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
            reason="Handle the next message",
            process_metadata={**recorded_tree(), "invoked_python": bad},
            relay_provider="codex",
            relay_thread_id=str(uuid4()),
            authority=config_authority,
            local_machine=local_machine,
            name="Test name",
            executor_name="Test executor_name",
        )


async def _termination_session(
    conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    status: str = "active",
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    local_machine: str,
) -> tuple[RuntimeIncarnation, dict[str, Any]]:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling',%s)",
        (agent_id, local_machine),
    )
    conn.commit()
    owner = await admit_hosted_runtime(
        aops_pool,
        agent_id,
        local_machine,
        uuid4(),
        db=Database.from_settings(gate=database_gate),
    )
    assert owner is not None
    session = leases.request(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        ttl_seconds=300,
        reason="Continue the work",
        name="Interruption test",
        executor_name="test executor",
        process_metadata=recorded_tree(),
        relay_provider="claude",
        authority=config_authority,
        local_machine=local_machine,
    )
    if status in ("accepted", "active"):
        leases.prepare(
            Database.from_settings(gate=database_gate),
            EventBus.from_settings(),
            session["id"],
            agent_id,
            owner,
            "Handoff brief",
            local_machine=local_machine,
        )
    if status == "active":
        leases.activate(
            Database.from_settings(gate=database_gate),
            EventBus.from_settings(),
            session["id"],
            owner,
        )
    return owner, session


def _native_notices(conn: psycopg.Connection, agent_id: int) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT id,content,payload->>'note_tag',status,created_at FROM inbound_messages "
        "WHERE agent_id=%s AND source='system:impersonation' ORDER BY created_at,id",
        (agent_id,),
    ).fetchall()


async def _terminate_native(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    owner: RuntimeIncarnation,
    session: dict[str, Any],
    runtime: Runtime[MeivaContext],
    mode: str,
    database_gate: ProcessDbGate,
    *,
    local_machine: str,
) -> None:
    with pool(
        max_size=2,
        gate=database_gate,
    ) as ops_pool:
        if mode == "force":
            force_terminate_transaction(owner.agent_id, ops_pool, source="user")
        else:
            terminate_id, _cutoff = enqueue_termination_inbounds(
                Database.from_settings(gate=database_gate),
                EventBus.from_settings(),
                owner.agent_id,
                ops_pool,
                source="user",
                message=None,
            )
            batch = await claim_inbound_batch(
                aops_pool, owner.agent_id, lifecycle_only=True, incarnation=owner
            )
            assert [item.id for item in batch] == [terminate_id]
            assert _native_notices(db_conn, owner.agent_id) == []
            applied = await apply_hosted_lifecycle(aops_pool, owner, resume_turn=False)
            assert applied is not None and applied.kind == "terminate"
    notices = _native_notices(db_conn, owner.agent_id)
    assert [row[2] for row in notices] == ["impersonation", "lifecycle_terminate"]
    assert all(row[3] == "pending" for row in notices)
    assert "was interrupted because this agent was terminated" in notices[0][1]
    assert notices[1][1] == "You were terminated."
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (owner.agent_id,)
    ).fetchone() == ("terminated",)


@pytest.mark.parametrize("mode", ["drained", "force", "delayed_resurrection"])
async def test_termination_notices_precede_resurrection_in_native_claim(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    mode: str,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    local_machine = machine_identity.name()
    owner, session = await _termination_session(
        db_conn,
        aops_pool,
        status="active",
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=local_machine,
    )
    runtime = Runtime(
        context=MeivaContext(
            ops_pool=aops_pool,
            event_publisher=MagicMock(),
            agent=AgentSlices.resolve(
                default_reader=defaults(),
            ),
            db=Database.from_settings(gate=database_gate),
            bus=EventBus.from_settings(),
            identity=AgentIdentity(agent_id=owner.agent_id, owns_loop=True),
            original_incarnation=owner,
            clock_factory=Clock.from_settings,
        )
    )
    await _terminate_native(
        db_conn,
        aops_pool,
        owner,
        session,
        runtime,
        mode,
        database_gate=database_gate,
        local_machine=local_machine,
    )
    if mode == "delayed_resurrection":
        _age_and_sweep_notices(db_conn, owner.agent_id, database_gate=database_gate)
    resurrect_agent(
        database,
        event_bus,
        owner.agent_id,
        resurrected_by="user",
    )
    successor = await admit_hosted_runtime(
        aops_pool,
        owner.agent_id,
        local_machine,
        uuid4(),
        db=database,
    )
    assert successor is not None
    runtime = Runtime(context=replace(runtime.context, original_incarnation=successor))
    builder: Any = StateGraph(BaseAgentState, context_schema=MeivaContext)
    builder.add_node("claim", claim_node, destinations=("__end__",))
    builder.set_entry_point("claim")
    graph = builder.compile(checkpointer=InMemorySaver())
    assert not await settle_checkpoint(
        graph,
        database,
        event_bus,
        owner.agent_id,
        runtime.context.relays,
        incarnation=successor,
        timestamps=TimestampSource(clock=Clock.from_settings(), timestamps_enabled=False),
        host=process_host(machine_identity.name(), rng=_rng()),
    )
    resumed = await claim_node(
        BaseAgentState(), runtime, {"configurable": {"thread_id": str(owner.agent_id)}}
    )
    update = cast(dict[str, Any], resumed.update)
    assert isinstance(update, dict)
    messages = cast(list[BaseMessage], update["messages"])
    assert [message.additional_kwargs["meiva_note_tag"] for message in messages] == [
        "impersonation",
        "lifecycle_terminate",
        "lifecycle_resurrect",
    ]
    assert "was interrupted" in messages[0].content
    assert "You were terminated" in messages[1].content
    assert "You have been resurrected" in messages[2].content


def _age_and_sweep_notices(
    conn: psycopg.Connection, agent_id: int, *, database_gate: ProcessDbGate
) -> None:
    from services.wake.delivery_watchdog.dead_letter import dead_letter_stale_pending_terminated

    conn.execute(
        "UPDATE inbound_messages SET created_at=created_at-interval '2 days' "
        "WHERE agent_id=%s AND kind='system_note'",
        (agent_id,),
    )
    conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source,created_at) "
        "VALUES(%s,'ordinary stale notice','system_note','system',now()-interval '2 days')",
        (agent_id,),
    )
    conn.commit()
    with pool(max_size=1, gate=database_gate) as watchdog_pool:
        assert dead_letter_stale_pending_terminated(watchdog_pool, 86400) == 1
    assert all(row[3] == "pending" for row in _native_notices(conn, agent_id))


@pytest.mark.parametrize("status", ["requested", "accepted", "active"])
async def test_termination_closes_automatic_preparation_and_dismisses_reminders(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    status: str,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    local_machine = machine_identity.name()
    owner, session = await _termination_session(
        db_conn,
        aops_pool,
        status=status,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=local_machine,
    )
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source,payload) "
        "VALUES(%s,'renew','reminder','system',jsonb_build_object('lease_id',%s::text))",
        (owner.agent_id, session["id"]),
    )
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    notices = _native_notices(db_conn, owner.agent_id)
    assert len(notices) == 2
    assert db_conn.execute(
        "SELECT status,rejection_reason,summary_inbound_id FROM agent_impersonations WHERE id=%s",
        (session["id"],),
    ).fetchone() == ("expired", "terminated: agent was terminated", notices[0][0])
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE agent_id=%s AND kind='reminder'",
        (owner.agent_id,),
    ).fetchone() == ("done",)


async def test_termination_notice_rollback_and_repeated_status_writes(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    local_machine = machine_identity.name()
    owner, session = await _termination_session(
        db_conn,
        aops_pool,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=local_machine,
    )
    with db_conn.transaction(force_rollback=True):
        db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (owner.agent_id,))
        assert len(_native_notices(db_conn, owner.agent_id)) == 2
    assert _native_notices(db_conn, owner.agent_id) == []
    assert db_conn.execute(
        "SELECT status FROM agent_impersonations WHERE id=%s", (session["id"],)
    ).fetchone() == ("active",)
    for _ in range(2):
        db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (owner.agent_id,))
        db_conn.commit()
    assert len(_native_notices(db_conn, owner.agent_id)) == 2


async def test_restart_and_termination_without_a_lease_add_no_notices(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    local_machine = machine_identity.name()
    owner, session = await _termination_session(
        db_conn,
        aops_pool,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=local_machine,
    )
    db_conn.execute("UPDATE agents_meta SET status='idling' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    assert _native_notices(db_conn, owner.agent_id) == []
    assert db_conn.execute(
        "SELECT status,rejection_reason FROM agent_impersonations WHERE id=%s", (session["id"],)
    ).fetchone() == ("active", None)
    ordinary_id = create_agent(db_conn)
    db_conn.execute("INSERT INTO agents_meta(id,status) VALUES(%s,'running')", (ordinary_id,))
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (ordinary_id,))
    db_conn.commit()
    assert (
        db_conn.execute(
            "SELECT id FROM inbound_messages WHERE agent_id=%s", (ordinary_id,)
        ).fetchall()
        == []
    )


# meiva:note
# What the controller session reads on its stdout when the relay starts on an ended lease.
def _assert_interruption_pushed(emitted: str, session: dict[str, Any]) -> None:
    assert emitted.count("Meiva impersonation lease") == 1
    assert f"Meiva impersonation lease {session['session_id']}" in emitted
    assert session["id"] in emitted
    assert "Ended at:" in emitted
    assert "does not end or cancel any newer" in emitted
    assert "The agent was terminated" in emitted
    assert "no active native runtime is implied" in emitted
    assert "ACK" not in emitted


@pytest.mark.parametrize("transport_dead", [False, True])
async def test_terminal_relay_start_delivers_interruption_best_effort(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    transport_dead: bool,
    database: Database,
    redis_url: str,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    local_machine = machine_identity.name()
    from cli.parsers import build_parser

    owner, session = await _termination_session(
        db_conn,
        aops_pool,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=local_machine,
    )
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    # meiva:note
    # A controller session reads the relay's pushes from its stdout; a transport that is gone is
    # a stream that no longer accepts writes.
    transport = io.StringIO()
    if transport_dead:
        transport.close()
    lease_before = db_conn.execute(
        "SELECT status, expires_at FROM agent_impersonations WHERE id=%s", (session["id"],)
    ).fetchone()
    args = build_parser().parse_args(
        [
            "impersonate",
            "relay",
            str(owner.agent_id),
            "--lease-id",
            session["id"],
            "--provider",
            "claude",
        ]
    )
    import asyncio

    real_clock = Clock(
        ClockConfig("UTC", None, message_timestamp_weekday=False), timers=SYSTEM_TIMERS
    )
    result = await asyncio.to_thread(
        relay.cmd_relay,
        args,
        inputs=relay.RelayInputs(
            session["relay_token"], redis_url, None, transport, bus=event_bus, clock=real_clock
        ),
        database_factory=lambda: database,
        machine=machine_identity,
    )
    assert result == (1 if transport_dead else 0)
    if not transport_dead:
        _assert_interruption_pushed(transport.getvalue(), session)
    assert len(_native_notices(db_conn, owner.agent_id)) == 2
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (owner.agent_id,)
    ).fetchone() == ("terminated",)
    # meiva:note
    # A terminal relay neither reads nor reserves an ordinary inbox (no per-message delivery row
    # exists for the lease) and does not renew the lease it found ended.
    assert db_conn.execute(
        "SELECT count(*) FROM agent_impersonation_messages WHERE lease_id=%s", (session["id"],)
    ).fetchone() == (0,)
    assert (
        db_conn.execute(
            "SELECT status, expires_at FROM agent_impersonations WHERE id=%s", (session["id"],)
        ).fetchone()
        == lease_before
    )


async def test_running_relay_delivers_termination_once_without_reserving_input(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
    redis_url: str,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    import asyncio

    local_machine = machine_identity.name()
    owner, session = await _termination_session(
        db_conn,
        aops_pool,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=local_machine,
    )
    transport = io.StringIO()
    relay_task = asyncio.create_task(
        relay.push_inbox(
            relay.RelayLease(
                agent_id=owner.agent_id,
                lease_id=UUID(session["id"]),
                session_id=session["session_id"],
                token=session["relay_token"],
                machine=local_machine,
            ),
            db=database,
            bus=event_bus,
            listener=RedisInboundListener(redis_url, owner.agent_id),
            adapter=ClaudeStdout(transport),
            tuning=RelayTuning(debounce=0.0, max_chars=None, catchup_seconds=0.2),
            clock=Clock(
                ClockConfig("UTC", None, message_timestamp_weekday=False), timers=SYSTEM_TIMERS
            ),
        )
    )
    try:
        while "Meiva control active:" not in transport.getvalue():
            assert not relay_task.done(), transport.getvalue()
            await asyncio.sleep(0.02)
        db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (owner.agent_id,))
        db_conn.commit()
        await asyncio.wait_for(relay_task, timeout=60)
    finally:
        relay_task.cancel()
    emitted = transport.getvalue()
    assert emitted.startswith("Handoff brief\n\nMeiva control active:")
    assert emitted.count("Meiva impersonation lease") == 1
    assert f"Meiva impersonation lease {session['session_id']}" in emitted
    assert session["id"] in emitted
    assert "Ended at:" in emitted
    assert "does not end or cancel any newer" in emitted
    assert "The agent was terminated" in emitted
    assert "no active native runtime is implied" in emitted
    assert db_conn.execute(
        "SELECT count(*) FROM agent_impersonation_messages WHERE lease_id=%s", (session["id"],)
    ).fetchone() == (0,)


async def test_resurrection_timestamp_follows_notes_even_in_an_older_transaction(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    local_machine = machine_identity.name()
    owner, _session = await _termination_session(
        db_conn,
        aops_pool,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=local_machine,
    )
    with pool(max_size=1, gate=database_gate) as other, other.connection() as conn:
        conn.execute(
            "SELECT 1 FROM agents_meta WHERE id=%s FOR NO KEY UPDATE",
            (owner.agent_id,),
        )
        resurrect_task = asyncio.create_task(
            asyncio.to_thread(
                resurrect_agent,
                database,
                event_bus,
                owner.agent_id,
                resurrected_by="user",
                prompt="Continue",
            )
        )
        while db_conn.execute("SELECT 1 FROM pg_locks WHERE NOT granted").fetchone() is None:
            db_conn.rollback()
            assert not resurrect_task.done()
            await asyncio.sleep(0.01)
        db_conn.rollback()
        conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (owner.agent_id,))
        conn.commit()
        await asyncio.wait_for(resurrect_task, timeout=15)
    ordered = db_conn.execute(
        "SELECT kind,payload->>'note_tag' FROM inbound_messages "
        "WHERE agent_id=%s ORDER BY created_at,id",
        (owner.agent_id,),
    ).fetchall()
    assert ordered == [
        ("system_note", "impersonation"),
        ("system_note", "lifecycle_terminate"),
        ("resurrect", None),
        ("chat", None),
    ]
