from __future__ import annotations

import time

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig

from agent.graph.llm._backoff import Attempt
from agent.graph.llm.node import llm_attempt
from agent.graph.llm.tests.llm_support import ScriptedModel, llm_runtime, tool_call_chunk
from agent.state import AgentState
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from base.log.tests.log_records import LogRecords

_CONFIG: RunnableConfig = {"configurable": {"thread_id": "1"}}
_STATE = AgentState(messages=[HumanMessage(content="hi")], halted=False)


# meiva:note
# A tool call keeps the turn going, so that attempt emits its own record — the halt record
# is the turn boundary's (agent/graph/claim/turn_end.py), which stamps the same agent id.
async def test_turn_end_records_carry_the_owning_agent(
    loguru_records: LogRecords, model_catalog: ModelCatalog, database_gate: ProcessDbGate
) -> None:
    model = ScriptedModel(scripts=[[tool_call_chunk()]])

    result = await llm_attempt(
        _STATE,
        llm_runtime(model, model_catalog=model_catalog, database_gate=database_gate),
        _CONFIG,
        Attempt(1, time.time()),
    )
    assert result is not None

    records = loguru_records.events("turn_end")
    assert len(records) == 1
    assert records[0]["extra"]["ok"] is True
    assert records[0]["extra"]["agent_id"] == 1


async def test_turn_end_failure_records_carry_the_owning_agent(
    loguru_records: LogRecords, model_catalog: ModelCatalog, database_gate: ProcessDbGate
) -> None:
    model = ScriptedModel(scripts=[[RuntimeError("simulated provider failure")]])

    with pytest.raises(RuntimeError, match="simulated provider failure"):
        await llm_attempt(
            _STATE,
            llm_runtime(model, model_catalog=model_catalog, database_gate=database_gate),
            _CONFIG,
            Attempt(1, time.time()),
        )

    records = loguru_records.events("turn_end")
    assert len(records) == 1
    assert records[0]["extra"]["ok"] is False
    assert records[0]["extra"]["agent_id"] == 1


async def test_halt_record_carries_the_owning_agent(
    loguru_records: LogRecords, model_catalog: ModelCatalog, database_gate: ProcessDbGate
) -> None:
    empty = AIMessageChunk(
        content="",
        response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
        usage_metadata={"input_tokens": 1, "output_tokens": 0, "total_tokens": 1},
    )
    model = ScriptedModel(scripts=[[empty]])

    result = await llm_attempt(
        _STATE,
        llm_runtime(model, model_catalog=model_catalog, database_gate=database_gate),
        _CONFIG,
        Attempt(1, time.time()),
    )
    assert result is not None

    records = loguru_records.events("halt")
    assert len(records) == 1
    assert records[0]["extra"]["agent_id"] == 1
