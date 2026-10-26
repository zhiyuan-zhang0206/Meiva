import json
import random
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Annotated, Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.graph import START, StateGraph
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel
from redis.client import PubSub

from agent import impersonation
from agent import state as states
from agent.graph.claim.node import claim_node
from agent.graph.exec.node import exec_node
from agent.graph.exec.process import SubprocessExecProcess, read_parent_facts
from agent.impersonation import protect_native_hooks, settle_checkpoint
from agent.impersonation_handoff import flush_checkpoint
from agent.ownership.hosted import admit_hosted_runtime
from agent.takeover.relay import RelayWait, TakeoverHost
from agent.tests.impersonation.lease_support import fixed_clock, relay_program, throttled_saver
from base.agents import impersonation as leases
from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.agents.history.checkpoint_postgres_walks import HistoryAsyncPostgresSaver
from base.agents.impersonation.tests.impersonation_support import recorded_tree
from base.agents.inbox.insert import insert_inbound_message
from base.agents.messages.timestamp_policy import TimestampSource
from base.agents.observation.relay_supervision import RelaySupervision
from base.api_contracts.caller_identity import CallerIdentity
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.clock.tests.seeded_rng import seeded_rng
from base.cluster import inbound_channel
from base.cluster.machine import MachineIdentityReader
from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import defaults, snapshot_process_config
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.packages.composer_commands import CommandCatalog
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions
from meiva.boot import derived_clients
from meiva.tests.fixtures.pin_agent import hosted_resources as hosted_resources


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws.
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


__all__ = [
    "HandoffState",
    "WakeLog",
    "assert_death_cause_note",
    "assert_end_note_and_handoff_document",
    "assert_handoff_precedes_queued_input",
    "assert_resume_note_delivery_contract",
    "assert_resumed_transcript",
    "assert_successor_handoff_preserves_unacknowledged_input",
    "assert_unacknowledged_input_preserved",
    "controller_relay_alive",
    "deliver_peers_and_ack_first",
    "end_external_session",
    "executor_acks_says_and_releases",
    "handoff_clients",
    "handoff_timestamps",
    "inbound_wakes",
    "local_exec_process",
    "prepare_graph",
    "resume_native",
    "runner_exec_process",
    "stop_takeover_with_exhausted_ack",
    "stop_takeover_with_lost_relay",
    "takeover_host",
]


@pytest.fixture
def handoff_clients(machine_identity: MachineIdentityReader) -> Iterator[ClientSet]:
    clients = derived_clients(snapshot_process_config(), machine_identity)
    try:
        yield clients
    finally:
        clients.close()


def handoff_timestamps() -> TimestampSource:
    clock = Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS)
    return TimestampSource(clock=clock, timestamps_enabled=False)


def takeover_host(tmp_path: Path, machine: MachineIdentityReader) -> TakeoverHost:
    return TakeoverHost(
        local_machine=machine.name(),
        workspaces_root=tmp_path / "workspaces",
        relay_program=relay_program(tmp_path / "relay", "exit:9"),
        wait=RelayWait(timeout_s=5.0, poll_s=0.05),
        rng=_rng(),
        clock=fixed_clock(),
    )


def controller_relay_alive(db_conn: psycopg.Connection[Any], lease_id: str) -> None:
    db_conn.execute(
        "UPDATE agent_impersonations SET relay_heartbeat_at=clock_timestamp() WHERE id=%s",
        (lease_id,),
    )
    db_conn.commit()


class WakeLog:
    def __init__(self, pubsub: PubSub) -> None:
        self._pubsub = pubsub

    def drain(self) -> list[str]:
        received: list[str] = []
        while message := cast("dict[str, Any] | None", self._pubsub.get_message(timeout=0.2)):
            if message["type"] == "message":
                received.append(str(message["data"]))
        return received


@contextmanager
def inbound_wakes(event_bus: EventBus, agent_id: int) -> Generator[WakeLog]:
    with (
        event_bus.sync_redis(decode_responses=True) as redis,
        PubSub(redis.connection_pool) as pubsub,
    ):
        pubsub.subscribe(inbound_channel(agent_id, event_bus.channel))
        subscribed = cast("dict[str, Any] | None", pubsub.get_message(timeout=2))
        assert subscribed is not None and subscribed["type"] == "subscribe"
        yield WakeLog(pubsub)


def _add(left: int, right: int) -> int:
    return left + right


class HandoffState(BaseModel):
    total: Annotated[int, _add] = 0


# meiva:note
# An exec process whose children connect to the database as the runner login: the child
# environment is the parent's facts with the runner URL, so no process environment changes.
def runner_exec_process(runner_db_url: str) -> SubprocessExecProcess:
    facts = read_parent_facts()
    return SubprocessExecProcess(
        replace(facts, environ={**facts.environ, "MEIVA_DB_URL": runner_db_url}), rng=_rng()
    )


def local_exec_process() -> SubprocessExecProcess:
    return SubprocessExecProcess(read_parent_facts(), rng=_rng())


async def prepare_graph(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    *,
    exec_process: SubprocessExecProcess,
    config_authority: ConfigAuthority,
    clients: ClientSet,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> tuple[
    Any,
    HistoryAsyncPostgresSaver,
    MeivaContext,
    RunnableConfig,
    dict[str, Any],
    RuntimeIncarnation,
    dict[str, Any],
    list[Any],
]:
    registry = ExtensionRegistry((("handoff", PluginContributions(state=(HandoffState,))),))
    state_cls = states.build_agent_state(registry)
    agent_id = create_agent(db_conn)
    machine = machine_identity.name()
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling',%s)", (agent_id, machine)
    )
    db_conn.commit()
    owner = await admit_hosted_runtime(
        aops_pool,
        agent_id,
        machine,
        uuid4(),
        db=Database.from_settings(gate=database_gate),
    )
    assert owner is not None
    requested = leases.request(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(rng=seeded_rng()),
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        process_metadata=recorded_tree(),
        reason="Do the task",
        name="Integration test",
        executor_name="Codex: integration",
        relay_provider="claude",
        authority=config_authority,
        local_machine=machine_identity.name(),
    )
    model_calls: list[Any] = []

    async def model(state: Any) -> Command[str]:
        model_calls.append(state)
        return Command(update={"halted": True}, goto="claim")

    async def before_llm(*_args: Any) -> Command[Any]:
        return Command(goto="llm")

    saver = await throttled_saver(aops_pool)
    builder: Any = StateGraph(state_cls, context_schema=MeivaContext)
    builder.add_node("claim", claim_node, destinations=("before_llm", "__end__", "claim"))
    builder.add_node(
        "before_llm", protect_native_hooks(before_llm), destinations=("llm", "__end__")
    )
    builder.add_node("llm", model, destinations=("exec", "claim"))
    builder.add_node(
        "exec",
        partial(exec_node, process=exec_process),
        destinations=("after_exec",),
    )

    def after_exec(_state: Any) -> Command[str]:
        return Command(goto="claim")

    builder.add_node("after_exec", after_exec, destinations=("claim",))
    builder.add_edge(START, "claim")
    graph = builder.compile(checkpointer=saver)
    ctx = MeivaContext(
        machine=machine_identity,
        ops_pool=aops_pool,
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(default_reader=defaults()),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(rng=seeded_rng()),
        clients=clients,
        identity=AgentIdentity(agent_id=agent_id, owns_loop=True),
        original_incarnation=owner,
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
        commands=CommandCatalog.system(skills=list),
    )
    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}, "recursion_limit": 100}
    reset: dict[str, Any] = {
        "turn_active": False,
        "turn_idle": False,
        "exit_requested": False,
        "restart_requested": False,
    }
    return graph, saver, ctx, config, reset, owner, requested, model_calls


def deliver_peers_and_ack_first(
    db_conn: psycopg.Connection[Any],
    database: Database,
    event_bus: EventBus,
    requested: dict[str, Any],
    agent_id: int,
    *,
    machine_identity: MachineIdentityReader,
) -> int:
    first_peer = insert_inbound_message(
        db_conn,
        agent_id,
        "Peer message acknowledged",
        source="agent:99",
        bus=event_bus,
        database=database,
    )
    second_peer = insert_inbound_message(
        db_conn,
        agent_id,
        "Peer message still pending",
        source="agent:99",
        bus=event_bus,
        database=database,
    )
    inbox = leases.inbox(
        database,
        requested["id"],
        local_machine=machine_identity.name(),
    )
    assert {row["id"] for row in inbox} == {first_peer, second_peer}
    leases.ack(
        database,
        event_bus,
        requested["id"],
        [first_peer],
        local_machine=machine_identity.name(),
    )
    return second_peer


def end_external_session(
    db_conn: psycopg.Connection[Any],
    database: Database,
    event_bus: EventBus,
    requested: dict[str, Any],
    finish: str,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    if finish == "release":
        leases.release(
            database,
            event_bus,
            requested["id"],
            "External work complete",
            local_machine=machine_identity.name(),
        )
        return
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
        (requested["id"],),
    )
    db_conn.commit()
    with pytest.raises(leases.ImpersonationError, match="expired"):
        leases.require_active(
            database,
            requested["id"],
            local_machine=machine_identity.name(),
        )


def assert_resumed_transcript(resumed: dict[str, Any], finish: str) -> None:
    transcript = "\n".join(str(message.content) for message in resumed["messages"])
    assert ("External work complete" if finish == "release" else "expired") in transcript
    assert "Peer message still pending" not in transcript
    assert "impersonation/0.json" in transcript
    assert "Peer message acknowledged" not in transcript


def executor_acks_says_and_releases(
    database: Database,
    event_bus: EventBus,
    requested: dict[str, Any],
    inbound_id: int,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    from base.agents.impersonation import history as history

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
    history.say(
        database,
        event_bus,
        requested["id"],
        "Work completed",
        message_key="result",
        local_machine=machine_identity.name(),
    )
    leases.release(
        database,
        event_bus,
        requested["id"],
        "Fixed login and verified the result.",
        local_machine=machine_identity.name(),
    )


def assert_end_note_and_handoff_document(last: Any, workspace: Path) -> None:
    handoff_path = workspace / "impersonation" / "0.json"
    assert last.additional_kwargs["meiva_note_tag"] == "impersonation"
    assert "Fixed login and verified" in last.content
    document = json.loads(handoff_path.read_text())
    assert [m["payload"]["content"] for m in document["messages"]] == [
        "During takeover",
        "Work completed",
    ]
    assert document["messages"][0]["acknowledged"]
    assert str(handoff_path) in last.content


def assert_handoff_precedes_queued_input(messages: list[Any]) -> None:
    handoff_index = next(
        i for i, m in enumerate(messages) if m.id.startswith("impersonation-handoff:")
    )
    next_index = next(i for i, m in enumerate(messages) if "Next task" in m.content)
    assert handoff_index < next_index


def assert_resume_note_delivery_contract(content: str) -> None:
    assert "External work complete" in content
    assert "structured handoff" not in content
    assert "The session record is available at:" in content
    assert "impersonation/0.json" in content
    assert "missing entries do not establish that an action never happened" in content
    assert "Continue any requests whose completion is not established" in content


async def stop_takeover_with_lost_relay(
    database: Database,
    event_bus: EventBus,
    owner: RuntimeIncarnation,
    relays: RelaySupervision,
) -> str:
    session = leases.native_status(database, event_bus, owner.agent_id, owner)
    assert session is not None
    await impersonation.supervise_relay(
        database,
        event_bus,
        session,
        owner.agent_id,
        relays,
        incarnation=owner,
        clock=fixed_clock(),
    )
    return "the bound relay stopped heartbeating"


def stop_takeover_with_exhausted_ack(
    db_conn: psycopg.Connection[Any], requested: dict[str, Any], owner: RuntimeIncarnation
) -> str:
    row = db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,kind,source,content) "
        "VALUES(%s,'chat','user','Preserve this unacknowledged input') RETURNING id",
        (owner.agent_id,),
    ).fetchone()
    assert row is not None
    db_conn.execute(
        "INSERT INTO agent_impersonation_messages(lease_id,inbound_id,delivery_attempts,last_delivery_at) "
        "VALUES(%s,%s,2,clock_timestamp()-interval '181 seconds')",
        (requested["id"], row[0]),
    )
    db_conn.commit()
    return (
        f"the executor did not ACK message {row[0]} after 2 delivery attempts (180s per ACK window)"
    )


def assert_death_cause_note(note: Any, agent_id: int, detail: str) -> None:
    assert note.id == f"impersonation-handoff:{agent_id}:0"
    assert f"This session was stopped early: {detail}." in note.content
    assert "structured handoff" not in note.content


def assert_unacknowledged_input_preserved(workspace: Path) -> None:
    handoff = (workspace / "impersonation/0.json").read_text()
    assert "Preserve this unacknowledged input" in handoff
    assert '"acknowledged": false' in handoff


def assert_successor_handoff_preserves_unacknowledged_input(
    resumed: dict[str, Any], workspace: Path
) -> None:
    transcript = "\n".join(str(message.content) for message in resumed["messages"])
    assert "native runtime restarted" in transcript
    assert "Peer message acknowledged" not in transcript
    handoff_path = workspace / "impersonation/0.json"
    assert str(handoff_path) in transcript
    handoff = json.loads(handoff_path.read_text())
    returned = next(
        row
        for row in handoff["messages"]
        if row["payload"]["content"] == "Peer message still pending"
    )
    assert returned["acknowledged"] is False


async def resume_native(
    graph: Any,
    saver: Any,
    ctx: Any,
    config: Any,
    reset: Any,
    owner: RuntimeIncarnation,
    model_calls: Any,
    wakes: WakeLog,
    database: Database,
    event_bus: EventBus,
    host: TakeoverHost,
) -> None:
    ctx = replace(ctx, original_incarnation=owner)
    wakes.drain()
    await graph.ainvoke(reset, config, context=ctx)
    assert not model_calls
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
    assert wakes.drain() == ["impersonation"]
    resumed = await graph.ainvoke(reset, config, context=ctx)
    await flush_checkpoint(saver, owner.agent_id)
    assert len(model_calls) == 1
    assert resumed["impersonation_handoff_id"] == f"{owner.agent_id}:0"
