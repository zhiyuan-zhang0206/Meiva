from __future__ import annotations

import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from agent.graph.llm.goto import LlmGoto
from agent.graph.llm.node import llm_node
from agent.graph.llm_errors import LlmLedger
from agent.graph.tests.test_llm_helpers import make_runtime
from agent.messages import exec_output_message
from agent.state import AgentState
from base.agents.messages.kwargs import ExecStatus
from base.db.code_version_gate import ProcessDbGate
from base.log.tests.log_records import LogRecords
from base.native_process.turn_identity import HostedTurnResources
from meiva.tests.fixtures.pin_agent import hosted_resources as hosted_resources

_NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


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


# meiva:note
# The tool return is built by the real envelope constructor, so the typed `meiva_exec_status`
# outcome under test is the one the exec node writes, not a shape invented here.
def _turn_that_ran_a_tool(
    status: ExecStatus = ExecStatus.COMPLETED, *, next_user_message: bool = False
) -> list[Any]:
    messages: list[Any] = [
        HumanMessage(content="check the deploy"),
        AIMessage(content="", tool_calls=[{"name": "execute_code", "args": {}, "id": "c1"}]),
        exec_output_message(
            content="ran in 1.2s\nok",
            tool_call_id="c1",
            status=status,
            body_start=11,
            created_at=_NOW,
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
) -> Command[LlmGoto]:
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
# `Command.update` is Optional in strict mode, so the verdict is read through one place: every test
# asserts the same field of the same shape instead of repeating a cast at each site.
def _halted(result: Command[LlmGoto]) -> Any:
    update: dict[str, Any] = result.update or {}
    return update.get("halted")


# meiva:note
# A completed tool run followed by reasoning-only output is a finished turn: the agent did its work
# and stopped, so the node halts (halted=True) instead of buying the extra call whose only possible
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

    assert result.goto == "after_exec"
    assert _halted(result) is True
    silent_idle = loguru_records.events("silent_idle")
    assert len(silent_idle) == 1, "exactly one verdict per turn"
    assert silent_idle[0]["extra"]["halted"] is True
    assert silent_idle[0]["extra"]["agent_id"] == 7
    assert "turn already acted" in silent_idle[0]["message"]
    assert ledger.silent_idle_output_tokens("7") == 0


# meiva:note
# Every exec outcome other than completed keeps the original nudge and cap. The envelope's typed
# `meiva_exec_status` is the marker, so a failed, timed-out, cancelled, halted or not-run call is
# never reported to the user as a silently finished turn, and no tool output is inspected as text.
@pytest.mark.parametrize(
    "status",
    [
        ExecStatus.FAILED,
        ExecStatus.TIMED_OUT,
        ExecStatus.CANCELLED,
        ExecStatus.HALTED,
        ExecStatus.NOT_RUN,
        pytest.param(ExecStatus.UNKNOWN, id="restart-repair-interrupted-placeholder"),
    ],
)
async def test_silent_idle_after_a_failed_tool_return_keeps_the_nudge(
    hosted_resources: HostedTurnResources,
    loguru_records: LogRecords,
    database_gate: ProcessDbGate,
    status: ExecStatus,
) -> None:
    ledger = LlmLedger()
    result = await _llm_turn(
        _turn_that_ran_a_tool(status),
        output_tokens=42,
        ledger=ledger,
        database_gate=database_gate,
        hosted_resources=hosted_resources,
    )

    assert _halted(result) is False
    silent_idle = loguru_records.events("silent_idle")
    assert len(silent_idle) == 1
    assert silent_idle[0]["extra"]["halted"] is False
    assert "turn already acted" not in silent_idle[0]["message"]
    assert ledger.silent_idle_output_tokens("7") == 42


# meiva:note
# Only the message this call follows counts, so the same completed tool return sitting earlier in
# the history under a fresh user message keeps the continue-loop and the cap behind it: a previous
# turn's work cannot excuse a turn that has done nothing, and the turn's own budget keeps
# accumulating.
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

    assert _halted(result) is False
    silent_idle = loguru_records.events("silent_idle")
    assert len(silent_idle) == 1
    assert silent_idle[0]["extra"]["halted"] is False
    assert "turn already acted" not in silent_idle[0]["message"]
    assert ledger.silent_idle_output_tokens("7") == 618
