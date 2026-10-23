"""The LLM node discards a partial turn when the cancel event wins its race with the stream."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import psycopg
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool

from agent.graph.interrupt.tests.queued_commands import queue_command
from agent.graph.llm.node import llm_node
from agent.graph.llm_errors import LlmLedger
from agent.graph.tests.test_llm_helpers import make_runtime
from agent.state import AgentState
from base.db import create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.projection import Cancelled
from base.events.live.tests.live_capture import LiveEvents
from base.native_process.stop_timing import RESTART_INTERRUPT_GRACE_S
from base.native_process.turn_identity import HostedTurnResources
from meiva.tests.fixtures.pin_agent import hosted_resources as hosted_resources


def _config(agent_id: int) -> RunnableConfig:
    return {"configurable": {"thread_id": str(agent_id)}}


def _committed_messages(update: dict[str, Any]) -> list[Any]:
    return update.get("messages", [])


def _has_cancelled(events: list[Any], agent_id: int) -> bool:
    return any(isinstance(e, Cancelled) and e.agent_id == agent_id for e in events)


async def _queue_later(
    db_conn: psycopg.Connection, agent_id: int, delay: float, kind: str, *, waited_s: float = 0.0
) -> None:
    await asyncio.sleep(delay)
    queue_command(db_conn, agent_id, kind, waited_s=waited_s)


# meiva:note
# A queued cancel is noticed by the watcher on its poll, so the stream hangs until then: the
# node is genuinely mid-stream when the command lands, and the cancel branch is the only way out.
# cancel arriving during the LLM stream → llm_node detects it via asyncio.wait
# race → discards the entire partial generation: Command(halted=True,
# goto=after_exec) commits no messages (generation has no side effects,
# nothing to retain).
async def test_llm_node_cancel_event_race_discards_partial(
    redis_url: str,
    hosted_resources: HostedTurnResources,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    live_events: LiveEvents,
    database_gate: ProcessDbGate,
) -> None:

    async def _stream_then_hang():
        yield AIMessageChunk(content="par")
        yield AIMessageChunk(content="tial code")
        await asyncio.Future()

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _stream_then_hang()
    agent_id = create_agent(db_conn)
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    trigger = asyncio.create_task(_queue_later(db_conn, agent_id, 0.1, "cancel"))
    runtime = make_runtime(
        resources=hosted_resources,
        llm=fake_llm,
        event_publisher=live_events.publisher,
        database_gate=database_gate,
        ops_pool=aops_pool,
    )
    result = await llm_node(state, runtime, _config(agent_id), ledger=LlmLedger())
    await trigger

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    update = result.update
    assert update is not None
    assert update["halted"] is True
    # Clean discard: no partial AIMessage, no marker
    assert _committed_messages(update) == []
    assert _has_cancelled(await live_events.typed(), agent_id)


# meiva:note
# A restart's interrupt drops the half-generated reply like any interrupt, and says on the state
# that the halted turn is unfinished: claim must resume it, not treat it as idle.
async def test_llm_node_restart_interrupt_discards_partial_and_marks_the_turn_unfinished(
    redis_url: str,
    hosted_resources: HostedTurnResources,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
) -> None:

    async def _stream_then_hang():
        yield AIMessageChunk(content="half a sentence")
        await asyncio.Future()

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _stream_then_hang()
    agent_id = create_agent(db_conn)
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    trigger = asyncio.create_task(
        _queue_later(db_conn, agent_id, 0.1, "restart", waited_s=RESTART_INTERRUPT_GRACE_S + 1)
    )
    runtime = make_runtime(
        resources=hosted_resources,
        llm=fake_llm,
        event_publisher=MagicMock(),
        database_gate=database_gate,
        ops_pool=aops_pool,
    )
    result = await llm_node(state, runtime, _config(agent_id), ledger=LlmLedger())
    await trigger

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    assert result.update == {"halted": True, "interrupted_by_restart": True}


# meiva:note
# cancel arrives mid tool_call stream → partial AIMessage is entirely
# discarded, so **no** tool_result debt is incurred: because no complete
# AIMessage with tool_use is committed, history has no "assistant.tool_calls
# must be followed by ToolMessage" protocol debt.
#
# This is the exact opposite of exec cancel (there tool_use is already
# committed, exec_node must supply a [cancelled by user] tool_result).
async def test_llm_node_cancel_event_race_discards_partial_tool_call(
    redis_url: str,
    hosted_resources: HostedTurnResources,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    live_events: LiveEvents,
    database_gate: ProcessDbGate,
) -> None:

    async def _stream_tool_call_then_hang():
        yield AIMessageChunk(content=[{"type": "text", "text": "I'll run that.", "index": 0}])
        yield AIMessageChunk(
            content=[
                {
                    "type": "tool_use",
                    "id": "call_cancelled",
                    "name": "execute_code",
                    "input": {},
                    "index": 1,
                }
            ],
            tool_call_chunks=[
                {"name": "execute_code", "args": "", "id": "call_cancelled", "index": 1},
            ],
        )
        yield AIMessageChunk(
            content=[],
            tool_call_chunks=[
                {"name": None, "args": '{"code": "print(1)', "id": None, "index": 1},
            ],
        )
        await asyncio.Future()

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _stream_tool_call_then_hang()
    agent_id = create_agent(db_conn)
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    trigger = asyncio.create_task(_queue_later(db_conn, agent_id, 0.1, "cancel"))
    runtime = make_runtime(
        resources=hosted_resources,
        llm=fake_llm,
        event_publisher=live_events.publisher,
        database_gate=database_gate,
        ops_pool=aops_pool,
    )
    result = await llm_node(state, runtime, _config(agent_id), ledger=LlmLedger())
    await trigger

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    update = result.update
    assert update is not None
    assert update["halted"] is True
    # Entirely discarded: neither partial AIMessage nor supplemental tool_result
    assert _committed_messages(update) == []
    assert _has_cancelled(await live_events.typed(), agent_id)


# meiva:note
# cancel set before LLM emits anything → llm_node returns Command(halted=True,
# goto=after_exec) without committing any message; does not raise CancelledError
# (which is BaseException and would silently exit the process via asyncio.run).
async def test_llm_node_cancel_event_race_no_partial_returns_halted(
    redis_url: str,
    hosted_resources: HostedTurnResources,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    live_events: LiveEvents,
    database_gate: ProcessDbGate,
) -> None:

    async def _hang_immediately():
        await asyncio.Future()
        yield  # pragma: no cover

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _hang_immediately()
    agent_id = create_agent(db_conn)
    state = AgentState(messages=[HumanMessage(content="hi")], halted=False)

    queue_command(db_conn, agent_id, "cancel")
    runtime = make_runtime(
        resources=hosted_resources,
        llm=fake_llm,
        event_publisher=live_events.publisher,
        database_gate=database_gate,
        ops_pool=aops_pool,
    )
    result = await llm_node(state, runtime, _config(agent_id), ledger=LlmLedger())
    assert _has_cancelled(await live_events.typed(), agent_id)

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    assert result.update is not None
    assert result.update["halted"] is True
    assert _committed_messages(result.update) == []


# meiva:note
# no interrupt queued, LLM stream completes normally → llm_node returns
# Command(goto=before_exec).
async def test_llm_node_cancel_event_race_normal_completion(
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
) -> None:

    async def _fast_stream() -> AsyncIterator[AIMessageChunk]:
        # tool_use must have non-empty tool_calls (validator enforces "protocol consistency"
        # to prevent recurrence of 169)
        yield AIMessageChunk(
            content="print('hi')",
            response_metadata={"model_provider": "anthropic", "stop_reason": "tool_use"},
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            tool_call_chunks=[
                {"name": "execute_code", "args": '{"code": "x"}', "id": "call_1", "index": 0},
            ],
        )

    fake_llm = MagicMock()
    fake_llm.astream.return_value = _fast_stream()
    state = AgentState(messages=[HumanMessage(content="go")], halted=False)

    runtime = make_runtime(resources=hosted_resources, llm=fake_llm, database_gate=database_gate)
    result = await llm_node(state, runtime, _config(7), ledger=LlmLedger())

    assert isinstance(result, Command)
    assert result.update is not None
    assert result.update["messages"][0].content == "print('hi')"  # pyright: ignore[reportUnknownMemberType]
