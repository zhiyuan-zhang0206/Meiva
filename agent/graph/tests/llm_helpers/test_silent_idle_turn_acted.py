from __future__ import annotations

import random
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from agent.graph.llm.node import llm_node
from agent.graph.llm_errors import LlmLedger
from agent.graph.tests.test_llm_helpers import make_runtime
from agent.state import AgentState
from base.db.code_version_gate import ProcessDbGate
from base.log.tests.log_records import LogRecords
from base.native_process.turn_identity import HostedTurnResources
from meiva.tests.fixtures.pin_agent import hosted_resources as hosted_resources


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws.
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


CONFIG: RunnableConfig = {"configurable": {"thread_id": "7"}}


def _reasoning_only(output_tokens: int) -> AsyncIterator[AIMessageChunk]:
    async def _gen() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(
            content="",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={
                "input_tokens": 10,
                "output_tokens": output_tokens,
                "total_tokens": 10 + output_tokens,
            },
        )

    return _gen()


def _turn_that_ran_a_tool(
    *, tool_error: bool = False, next_user_message: bool = False
) -> list[Any]:
    messages: list[Any] = [
        HumanMessage(content="check the deploy"),
        AIMessage(content="", tool_calls=[{"name": "execute_code", "args": {}, "id": "c1"}]),
        ToolMessage(
            content="boom: tool failed" if tool_error else "ok",
            tool_call_id="c1",
            status="error" if tool_error else "success",
        ),
    ]
    if next_user_message:
        messages.append(HumanMessage(content="still there?"))
    return messages


async def _llm_turn(
    messages: list[Any],
    *,
    output_tokens: int,
    ledger: LlmLedger,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
) -> Any:
    fake_llm = MagicMock()
    fake_llm.astream.return_value = _reasoning_only(output_tokens)
    return await llm_node(
        AgentState(messages=messages, halted=False),
        make_runtime(
            resources=hosted_resources,
            llm=fake_llm,
            event_publisher=MagicMock(),
            database_gate=database_gate,
        ),
        CONFIG,
        ledger=ledger,
        rng=_rng(),
    )


# meiva:note
# A tool return followed by reasoning-only output is a finished turn: the agent did its work and
# stopped, so the node halts (halted=True) instead of buying the extra call whose only possible
# output is text manufactured for the nudge. Halting also leaves no cumulative budget behind for
# the next turn. The record names its agent; every silent_idle event carried a NULL agent_id
# before #5801.
async def test_silent_idle_after_this_turns_tool_return_halts_silently(
    hosted_resources: HostedTurnResources,
    loguru_records: LogRecords,
    database_gate: ProcessDbGate,
) -> None:
    ledger = LlmLedger()
    result = await _llm_turn(
        _turn_that_ran_a_tool(),
        output_tokens=618,
        ledger=ledger,
        database_gate=database_gate,
        hosted_resources=hosted_resources,
    )

    assert isinstance(result, Command)
    assert result.goto == "after_exec"
    assert result.update["halted"] is True
    silent_idle = loguru_records.events("silent_idle")
    assert len(silent_idle) == 1, "exactly one verdict per turn"
    assert silent_idle[0]["extra"]["halted"] is True
    assert silent_idle[0]["extra"]["agent_id"] == 7
    assert "turn already acted" in silent_idle[0]["message"]
    assert ledger.silent_idle_output_tokens("7") == 0


# meiva:note
# A failed tool still counts as acting: LangChain returns a ToolMessage typed status="error" for it,
# so the rule keys on the message type and the model's own decision to stop after seeing the failure
# is not re-prompted as if nothing had happened.
async def test_silent_idle_after_a_failed_tool_return_halts_silently(
    hosted_resources: HostedTurnResources,
    loguru_records: LogRecords,
    database_gate: ProcessDbGate,
) -> None:
    ledger = LlmLedger()
    result = await _llm_turn(
        _turn_that_ran_a_tool(tool_error=True),
        output_tokens=42,
        ledger=ledger,
        database_gate=database_gate,
        hosted_resources=hosted_resources,
    )

    assert isinstance(result, Command)
    assert result.update["halted"] is True
    silent_idle = loguru_records.events("silent_idle")
    assert len(silent_idle) == 1
    assert "turn already acted" in silent_idle[0]["message"]


# meiva:note
# Only the message this call follows counts, so the same tool return sitting earlier in the history
# under a fresh user message keeps the continue-loop and the cap behind it: a previous turn's work
# cannot excuse a turn that has done nothing, and the turn's own budget keeps accumulating.
async def test_a_previous_turns_tool_return_does_not_excuse_a_stuck_turn(
    hosted_resources: HostedTurnResources,
    loguru_records: LogRecords,
    database_gate: ProcessDbGate,
) -> None:
    ledger = LlmLedger()
    result = await _llm_turn(
        _turn_that_ran_a_tool(next_user_message=True),
        output_tokens=618,
        ledger=ledger,
        database_gate=database_gate,
        hosted_resources=hosted_resources,
    )

    assert isinstance(result, Command)
    assert result.update["halted"] is False
    silent_idle = loguru_records.events("silent_idle")
    assert len(silent_idle) == 1
    assert silent_idle[0]["extra"]["halted"] is False
    assert "turn already acted" not in silent_idle[0]["message"]
    assert ledger.silent_idle_output_tokens("7") == 618
