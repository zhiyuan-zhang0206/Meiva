from __future__ import annotations

import random
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from agent.graph.llm.node import llm_node
from agent.graph.llm.tests.llm_support import ScriptedModel, end_turn_chunk, llm_runtime
from agent.state import AgentState
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from base.log.tests.log_records import LogRecords


# meiva:note
# Provider content shapes must preserve their final message and usage while ending after one request. A seeded jitter generator keeps the node deterministic if its retry path regresses.
@pytest.mark.parametrize(
    "reply",
    [
        end_turn_chunk("", output_tokens=0),
        end_turn_chunk("", output_tokens=211),
        AIMessageChunk(
            content=[{"type": "thinking", "thinking": "Finished.", "signature": "sig"}],
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 10, "output_tokens": 0, "total_tokens": 10},
        ),
        AIMessageChunk(
            content="",
            additional_kwargs={"reasoning_content": "Finished."},
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 5, "output_tokens": 0, "total_tokens": 5},
        ),
        AIMessageChunk(
            content=[
                {"type": "reasoning", "summary": []},
                {"type": "text", "text": "", "phase": "final_answer"},
            ],
            response_metadata={"model_provider": "openai", "status": "completed"},
            usage_metadata={
                "input_tokens": 56_341,
                "output_tokens": 14,
                "total_tokens": 56_355,
                "output_token_details": {"reasoning": 8},
            },
        ),
    ],
    ids=["empty", "hidden-reasoning", "thinking", "deepseek-reasoning", "openai-empty-final"],
)
async def test_completion_after_tool_result_ends_without_forced_output(
    reply: AIMessageChunk,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    loguru_records: LogRecords,
) -> None:
    model = ScriptedModel(scripts=[[reply], [end_turn_chunk("must not be requested")]])
    state = AgentState(
        messages=[
            HumanMessage(content="Record the result, then wait quietly."),
            AIMessage(
                content="",
                tool_calls=[{"name": "execute_code", "args": {"code": "print(323)"}, "id": "c1"}],
            ),
            ToolMessage(content="323", tool_call_id="c1"),
        ],
        halted=False,
    )
    result = await llm_node(
        state,
        llm_runtime(model, model_catalog=model_catalog, database_gate=database_gate),
        {"configurable": {"thread_id": "1"}},
        rng=random.Random(0),  # noqa: S311
    )
    assert isinstance(result.update, dict)
    update = cast(dict[str, Any], result.update)
    assert result.goto == "after_exec"
    assert update["halted"] is True
    messages: list[Any] = update["messages"]
    assert len(messages) == 1
    final = messages[0]
    assert isinstance(final, AIMessage)
    assert final.content == reply.content
    assert final.usage_metadata == reply.usage_metadata
    assert not final.tool_calls
    assert len(model.scripts) == 1
    assert not loguru_records.events("silent_idle")
    assert len(loguru_records.events("llm_usage")) == 1
    halts = loguru_records.events("halt")
    assert len(halts) == 1
    assert halts[0]["level"].name == "INFO"
