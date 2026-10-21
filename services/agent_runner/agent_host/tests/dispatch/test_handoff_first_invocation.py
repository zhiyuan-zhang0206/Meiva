# meiva:note
# A durable handoff is fresh hosted input without reviving halted work.

from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, StateGraph
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool

from agent import state as states
from agent.graph.claim.node import claim_node
from agent.messages import system_note_message
from agent.ownership.tests.ownership_support import admit_owner, agent_row, insert_lifecycle_command
from base.agents.context import MeivaContext
from base.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from base.agents.messages.kwargs import NoteTag
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.native_process.turn_identity import HostedTurnResources
from base.packages.composer_commands import CommandCatalog
from meiva.tests.fixtures.pin_agent import hosted_resources as hosted_resources
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.tests.host_policy import configured_machine, configured_policy


async def _checkpoint(
    pool: AsyncConnectionPool[Any], agent_id: int, boundary: str, calls: list[states.BaseAgentState]
) -> tuple[Any, AsyncPostgresSaver, RunnableConfig]:
    async def model(state: states.BaseAgentState) -> Command[Any]:
        calls.append(state)
        return Command(
            update={"halted": True, "messages": [AIMessage(content="Resumed reply")]}, goto="claim"
        )

    saver = AsyncPostgresSaver(pool)
    await saver.setup()
    wrap_saver_reads_with_delta_reconstruction(saver)
    builder: Any = StateGraph(states.AgentState, context_schema=MeivaContext)
    builder.add_node("claim", claim_node, destinations=("before_llm", "claim", "__end__"))
    builder.add_node("before_llm", model, destinations=("claim",))
    builder.add_edge(START, "claim")
    graph = builder.compile(checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}
    receipt = f"{agent_id}:0"
    note = system_note_message(content="Session ended", tag=NoteTag.IMPERSONATION)
    note.id = f"impersonation-handoff:{receipt}"
    messages: list[AnyMessage] = [note]
    if boundary == "completed":
        messages.append(AIMessage(content="Already resumed"))
    await graph.aupdate_state(
        config,
        {
            "messages": messages,
            "impersonation_handoff_id": receipt,
            "halted": boundary == "cancelled",
            "circuit": {"open": boundary == "breaker", "reason": "permanent"},
        },
    )
    return graph, saver, config


@pytest.mark.parametrize("boundary", ["pending", "completed", "cancelled", "cancel", "breaker"])
async def test_first_hosted_invocation_only_resumes_unhalted_pending_handoff(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    boundary: str,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    if boundary == "cancel":
        insert_lifecycle_command(db_conn, agent_id, "cancel")
    calls: list[states.BaseAgentState] = []
    graph, saver, config = await _checkpoint(aops_pool, agent_id, boundary, calls)
    policy = configured_policy()
    machine = configured_machine(name="claim-test")
    host = AgentHost(
        policy=policy,
        pool=aops_pool,
        checkpointer=saver,
        graph=graph,
        machine=machine,
        bus=event_bus,
        db=database,
        catalog=model_catalog,
        commands=CommandCatalog(skills=list),
    )
    ctx = MeivaContext(
        ops_pool=aops_pool,
        original_incarnation=owner,
        hosted_resources=hosted_resources,
        agent=AgentSlices.resolve(default_reader=policy.default_reader),
        machine=machine,
        db=database,
        bus=event_bus,
        event_publisher=MagicMock(),
        catalog=model_catalog,
        clock_factory=policy.clock_factory,
    )
    try:
        await host._invoke_until_done(agent_id, ctx)
        assert len(calls) == (1 if boundary == "pending" else 0)
        if calls:
            assert calls[0].messages[-1].id == f"impersonation-handoff:{agent_id}:0"
        await host._invoke_until_done(agent_id, ctx)
        assert len(calls) == (1 if boundary == "pending" else 0)
        if boundary in {"cancel", "cancelled"}:
            snapshot = await graph.aget_state(config)
            assert snapshot.values["halted"] is True
    finally:
        await host.aclose()
