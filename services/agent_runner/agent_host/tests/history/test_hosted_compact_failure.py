"""Compaction aborts survive a real hosted turn and its PostgreSQL flush."""

import asyncio
from typing import Any

import psycopg
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, StateGraph
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool

from agent import state as states
from agent.graph.claim.node import claim_node
from agent.graph.hooks.compact import COMPACT_MAX_ATTEMPTS, CompactionFailedError
from agent.impersonation_handoff import flush_checkpoint
from agent.startup import wrap_saver_writes_with_nstep_interval
from base.agents.context import MeivaContext
from base.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from base.agents.inbox.insert import insert_inbound_message
from base.db import Database
from base.events.live.bus import EventBus
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.tests.history import provider_down_model
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from services.agent_runner.agent_host.tests.runtime.host_support import EventTap, Rig, Row


def cold_reader(pool: AsyncConnectionPool[Any]) -> AsyncPostgresSaver:
    """Cold inspector saver that folds delta-written messages (#3180 write switch)."""
    reader = AsyncPostgresSaver(pool)
    wrap_saver_reads_with_delta_reconstruction(reader)
    return reader


async def prepare_graph(
    pool: AsyncConnectionPool[Any], agent: int, interval: int, replies: list[str]
) -> tuple[Any, AsyncPostgresSaver, RunnableConfig, list[HumanMessage | AIMessage]]:
    async def model(state: states.BaseAgentState) -> Command[Any]:
        assert not state.halted
        replies.append("continued")
        return Command(
            update={"halted": True, "messages": [AIMessage(content="continued")]}, goto="claim"
        )

    saver = AsyncPostgresSaver(pool)
    await saver.setup()
    wrap_saver_writes_with_nstep_interval(saver, interval)
    # Delta write model (#3180): fold delta-written messages on read (daemon parity).
    wrap_saver_reads_with_delta_reconstruction(saver)
    builder: Any = StateGraph(states.AgentState, context_schema=MeivaContext)
    builder.add_node("claim", claim_node, destinations=("before_llm", "claim", "__end__"))
    builder.add_node("before_llm", model, destinations=("claim",))
    builder.add_edge(START, "claim")
    graph = builder.compile(checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": str(agent)}}
    history: list[HumanMessage | AIMessage] = [
        HumanMessage(id="original-user", content="Keep this original request"),
        AIMessage(id="original-assistant", content="Keep this original answer"),
    ]
    await graph.aupdate_state(config, {"messages": history, "halted": False})
    await flush_checkpoint(saver, agent)
    return graph, saver, config, history


def _queue_compact_request(db_conn: psycopg.Connection, agent: int) -> int:
    row = db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) "
        "VALUES(%s,'','compact_request','user') RETURNING id",
        (agent,),
    ).fetchone()
    assert row is not None
    db_conn.commit()
    return row[0]


def _inbound_status(db_conn: psycopg.Connection, inbound_id: int) -> tuple[Any, ...] | None:
    return db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (inbound_id,)
    ).fetchone()


def _agent_status(db_conn: psycopg.Connection, agent: int) -> tuple[Any, ...] | None:
    return db_conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent,)).fetchone()


async def _assert_failure_is_visible_and_durable(
    db_conn: psycopg.Connection,
    pool: AsyncConnectionPool[Any],
    event_tap: EventTap,
    config: RunnableConfig,
    *,
    agent: int,
    compact_id: int,
    history: list[HumanMessage | AIMessage],
) -> None:
    [event] = await event_tap.events("error")
    assert event["agent_id"] == agent
    assert "CompactionFailedError" in event["content"]
    assert "history was preserved" in event["content"]
    cold = await cold_reader(pool).aget_tuple(config)
    assert cold is not None
    persisted = cold.checkpoint["channel_values"]
    assert persisted["halted"] is True
    assert persisted["messages"] == history
    assert _inbound_status(db_conn, compact_id) == ("done",)
    assert _agent_status(db_conn, agent) == ("idling",)


async def _assert_new_inbound_resumes_with_history(
    db_conn: psycopg.Connection,
    pool: AsyncConnectionPool[Any],
    config: RunnableConfig,
    *,
    compact_id: int,
    history: list[HumanMessage | AIMessage],
) -> None:
    resumed = await cold_reader(pool).aget_tuple(config)
    assert resumed is not None
    messages = resumed.checkpoint["channel_values"]["messages"]
    assert messages[:2] == history
    assert any(message.content == "continued" for message in messages)
    assert _inbound_status(db_conn, compact_id) == ("done",)


def _retained_compaction_failure(
    host: AgentHost, provider_error: RuntimeError
) -> CompactionFailedError:
    ((scope, failure),) = host._resource_service.failures
    assert scope.service is host._resource_service
    assert isinstance(failure, CompactionFailedError)
    assert failure.__cause__ is provider_error
    return failure


async def _close_host_with_original_failure(
    host: AgentHost, original: CompactionFailedError | None, provider_error: RuntimeError
) -> None:
    if original is None:
        await host.aclose()
        return
    with pytest.raises(CompactionFailedError) as observed:
        await host.aclose()
    assert observed.value is original
    assert observed.value.__cause__ is provider_error
    assert host.resources_joined


@pytest.mark.parametrize("interval", [1, 100])
async def test_compaction_failure_is_visible_durable_and_recovers_on_new_inbound(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool[Any],
    rig: Rig,
    event_tap: EventTap,
    interval: int,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent = 1
    rig.stored.write(agent, Row(status="idling"))
    provider_error = RuntimeError("compaction provider unavailable")
    provider_down_model.FAILURES[agent] = provider_error
    provider_down_model.CALLS.pop(agent, None)
    replies: list[str] = []

    graph, saver, config, history = await prepare_graph(aops_pool, agent, interval, replies)
    compact_id = _queue_compact_request(db_conn, agent)
    host = rig.host(
        {},
        graph,
        catalog=None,
        policy=configured_policy(llm_override=provider_down_model.OVERRIDE),
        checkpointer=saver,
        workload_pool=aops_pool,
        control_pool=aops_pool,
        bus=None,
    )
    original_failure: CompactionFailedError | None = None
    try:
        await asyncio.wait_for(host.run_turn(agent), 10)
        await _assert_failure_is_visible_and_durable(
            db_conn,
            aops_pool,
            event_tap,
            config,
            agent=agent,
            compact_id=compact_id,
            history=history,
        )
        assert not replies
        assert provider_down_model.CALLS[agent] == COMPACT_MAX_ATTEMPTS
        original_failure = _retained_compaction_failure(host, provider_error)

        insert_inbound_message(
            db_conn,
            agent,
            "Continue without compacting",
            "user",
            bus=event_bus,
            database=database,
        )
        await asyncio.wait_for(host.run_turn(agent), 10)
        assert replies == ["continued"]
        assert provider_down_model.CALLS[agent] == COMPACT_MAX_ATTEMPTS
        await _assert_new_inbound_resumes_with_history(
            db_conn, aops_pool, config, compact_id=compact_id, history=history
        )
    finally:
        await _close_host_with_original_failure(host, original_failure, provider_error)
