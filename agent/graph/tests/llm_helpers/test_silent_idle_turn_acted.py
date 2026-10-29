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
        rng=_rng(),
    )


# meiva:note
# `Command.update` is Optional in strict mode, so the verdict is read through one place: every test
# asserts the same field of the same shape instead of repeating a cast at each site.
def _halted(result: Command[LlmGoto]) -> Any:
    update: dict[str, Any] = result.update or {}
    return update.get("halted")


@pytest.mark.parametrize("status", list(ExecStatus))
@pytest.mark.parametrize("next_user_message", [False, True])
async def test_reasoning_only_completion_halts_for_every_tool_outcome(
    hosted_resources: HostedTurnResources,
    loguru_records: LogRecords,
    database_gate: ProcessDbGate,
    status: ExecStatus,
    next_user_message: bool,
) -> None:
    result = await _llm_turn(
        _turn_that_ran_a_tool(status, next_user_message=next_user_message),
        output_tokens=618,
        database_gate=database_gate,
        hosted_resources=hosted_resources,
    )

    assert result.goto == "after_exec"
    assert _halted(result) is True
    assert not loguru_records.events("silent_idle")
    assert len(loguru_records.events("llm_usage")) == 1
    halts = loguru_records.events("halt")
    assert len(halts) == 1
    assert halts[0]["extra"]["agent_id"] == 7
