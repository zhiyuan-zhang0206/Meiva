# meiva:note
# 161 / 165 incident: when DeepSeek service degraded, the stream started but never returned another chunk; the default anthropic SDK 600s overall timeout was the only safety net, leaving the agent silently stuck for 4-10 minutes; turn_end ok=False never even fired, so the frontend saw neither a stream nor an error. Fix: bound the model's `__anext__` with an owned `asyncio.timeout` (the agent's TTFT, inter-chunk and total bounds) that raises `LLMStreamStallTimeoutError`, so the failure is a logged turn end with a traceback.
from __future__ import annotations

import time
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig

from agent.graph.llm._backoff import Attempt
from agent.graph.llm._deadlines import StreamBounds
from agent.graph.llm._stream import consume_llm, consume_stream_with_stall_timeout, stream_bounds
from agent.graph.llm.node import llm_attempt
from agent.graph.llm.tests.llm_support import (
    ScriptedModel,
    StubHandler,
    end_turn_chunk,
    llm_runtime,
)
from agent.graph.llm_errors import LlmLedger, LLMStreamStallTimeoutError
from agent.state import AgentState
from base.config.tests.configuration import defaults
from base.db.code_version_gate import ProcessDbGate
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog

_CONFIG: RunnableConfig = {"configurable": {"thread_id": "7"}}
_STATE = AgentState(messages=[HumanMessage(content="hi")], halted=False)


async def _one_try(
    model: ScriptedModel, pins: dict[str, Any], model_catalog: ModelCatalog, gate: ProcessDbGate
) -> Any:
    runtime = llm_runtime(model, model_catalog=model_catalog, database_gate=gate, pins=pins)
    return await llm_attempt(_STATE, runtime, _CONFIG, Attempt(1, time.time()), LlmLedger())


async def test_stall_at_ttft_raises_with_ttft_marker(
    model_catalog: ModelCatalog, database_gate: ProcessDbGate
) -> None:
    model = ScriptedModel(scripts=[["hang"]])

    with pytest.raises(LLMStreamStallTimeoutError, match=r"stream stalled.*TTFT"):
        await _one_try(
            model, {"llm_stream_ttft_timeout_seconds": 0.05}, model_catalog, database_gate
        )


# meiva:note
# Emits N chunks then hangs: the message says "mid-stream after N chunks". Triage split: TTFT = server never connected, mid-stream = connected then died.
async def test_stall_mid_stream_raises_with_chunk_count(
    model_catalog: ModelCatalog, database_gate: ProcessDbGate
) -> None:
    model = ScriptedModel(
        scripts=[
            [
                AIMessageChunk(content="hello "),
                AIMessageChunk(content="world"),
                AIMessageChunk(content="!"),
                "hang",
            ]
        ]
    )
    pins = {"llm_stream_ttft_timeout_seconds": 10.0, "llm_stream_inter_chunk_timeout_seconds": 0.05}

    with pytest.raises(LLMStreamStallTimeoutError, match="mid-stream after 3 chunks"):
        await _one_try(model, pins, model_catalog, database_gate)


# meiva:note
# A stream whose chunks arrive within the bounds is not killed by the stall timeout: the node returns its Command. The 1.0s bounds are a test value, not the production default.
async def test_normal_stream_completes_no_stall_timeout(
    model_catalog: ModelCatalog, database_gate: ProcessDbGate
) -> None:
    model = ScriptedModel(scripts=[[end_turn_chunk()]])
    pins = {"llm_stream_ttft_timeout_seconds": 1.0, "llm_stream_inter_chunk_timeout_seconds": 1.0}

    result = await _one_try(model, pins, model_catalog, database_gate)

    assert result.update["messages"][0].content == "ok"


# meiva:note
# A drip-fed stream cannot evade the per-attempt total-duration ceiling, and no second request follows it.
async def test_total_timeout_ends_the_original_request_while_chunks_keep_arriving(
    model_catalog: ModelCatalog,
) -> None:
    drip = [step for _ in range(40) for step in (AIMessageChunk(content="drip"), 0.05)]
    chunks: list[AIMessageChunk] = []

    with pytest.raises(LLMStreamStallTimeoutError, match="total duration"):
        await consume_llm(
            ScriptedModel(scripts=[drip]),
            [],
            chunks=chunks,
            handler=StubHandler(),
            agent=AgentSlices.resolve(
                default_reader=defaults(
                    lm__llm_stream_total_timeout_seconds=0.12,
                    lm__llm_stream_ttft_timeout_seconds=10.0,
                    lm__llm_stream_inter_chunk_timeout_seconds=10.0,
                )
            ),
            catalog=model_catalog,
        )

    assert 1 <= len(chunks) <= 3


async def test_stream_below_total_timeout_is_unchanged() -> None:
    chunks: list[AIMessageChunk] = []
    model = ScriptedModel(scripts=[[AIMessageChunk(content="a"), AIMessageChunk(content="b")]])

    await consume_stream_with_stall_timeout(
        model.astream([]).__aiter__(),
        chunks=chunks,
        handler=StubHandler(),
        bounds=StreamBounds(ttft_seconds=10.0, inter_chunk_seconds=10.0, total_seconds=10.0),
    )

    assert [chunk.content for chunk in chunks][:2] == ["a", "b"]


async def test_none_disables_stream_total_timeout() -> None:
    """The helper's None contract permits an intentionally unbounded caller."""
    slow = [step for _ in range(5) for step in (0.03, AIMessageChunk(content="still alive"))]
    chunks: list[AIMessageChunk] = []

    await consume_stream_with_stall_timeout(
        ScriptedModel(scripts=[slow]).astream([]).__aiter__(),
        chunks=chunks,
        handler=StubHandler(),
        bounds=StreamBounds(ttft_seconds=10.0, inter_chunk_seconds=10.0, total_seconds=None),
    )

    assert [chunk.content for chunk in chunks][:5] == ["still alive"] * 5


_STREAM_BOUNDS = {
    "llm_stream_ttft_timeout_seconds": "ttft_seconds",
    "llm_stream_total_timeout_seconds": "total_seconds",
    "llm_stream_inter_chunk_timeout_seconds": "inter_chunk_seconds",
}


@pytest.mark.parametrize("setting", sorted(_STREAM_BOUNDS))
def test_an_agents_stream_bound_is_the_one_the_stream_runs_under(
    setting: str, model_catalog: ModelCatalog
) -> None:
    pinned = stream_bounds(
        AgentSlices.resolve({setting: 4.25}, default_reader=defaults()), model_catalog
    )
    unpinned = stream_bounds(AgentSlices.resolve(default_reader=defaults()), model_catalog)

    assert getattr(pinned, _STREAM_BOUNDS[setting]) == 4.25
    for other in _STREAM_BOUNDS.values():
        if other != _STREAM_BOUNDS[setting]:
            assert getattr(pinned, other) == getattr(unpinned, other)
    assert getattr(unpinned, _STREAM_BOUNDS[setting]) != 4.25
