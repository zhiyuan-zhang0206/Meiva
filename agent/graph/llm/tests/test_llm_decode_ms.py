from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest
from google.genai.errors import ClientError
from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage

from agent.graph.llm._deadlines import StreamBounds
from agent.graph.llm._stream import consume_stream_with_stall_timeout, stream_llm
from agent.graph.llm.tests.llm_support import ScriptedModel, StubHandler, end_turn_chunk
from agent.llm import execute_code
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog

_BOUNDS = StreamBounds(ttft_seconds=10.0, inter_chunk_seconds=10.0, total_seconds=None)
_GAP = 0.05


async def test_consume_stream_records_first_and_last_arrival() -> None:
    async def stream() -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(content="a")
        await asyncio.sleep(_GAP)
        yield AIMessageChunk(content="b")
        await asyncio.sleep(_GAP)
        yield AIMessageChunk(content="c")

    chunks: list[AIMessageChunk] = []
    first_ts, last_ts = await consume_stream_with_stall_timeout(
        stream(), chunks=chunks, handler=StubHandler(), bounds=_BOUNDS
    )

    assert first_ts is not None and last_ts is not None
    assert 1.5 * _GAP <= last_ts - first_ts < 10.0
    assert [chunk.content for chunk in chunks] == ["a", "b", "c"]


async def test_consume_stream_empty_stream_returns_none() -> None:
    async def empty() -> AsyncIterator[AIMessageChunk]:
        if False:
            yield AIMessageChunk(content="x")

    chunks: list[AIMessageChunk] = []
    window = await consume_stream_with_stall_timeout(
        empty(), chunks=chunks, handler=StubHandler(), bounds=_BOUNDS
    )

    assert window == (None, None)
    assert chunks == []


async def test_stream_llm_stamps_latency_and_decode_window(model_catalog: ModelCatalog) -> None:
    model = ScriptedModel(scripts=[[AIMessageChunk(content="a"), _GAP, end_turn_chunk("b")]])
    handler = StubHandler()

    await stream_llm(
        model,
        [],
        chunks=[],
        handler=handler,
        agent=AgentSlices.resolve(default_reader=defaults()),
        catalog=model_catalog,
        monotonic=time.monotonic,
    )

    assert handler.llm_decode_ms is not None and handler.llm_latency_ms is not None
    assert handler.llm_decode_ms >= 0.9 * _GAP * 1000
    assert handler.llm_decode_ms <= handler.llm_latency_ms < 10_000


async def test_stream_preserves_prefix_and_does_not_recover_cache_403(
    model_catalog: ModelCatalog,
) -> None:
    failure = ClientError(
        403, {"error": {"message": "CachedContent not found or permission denied"}}
    )
    second_request = [end_turn_chunk("a second request would have streamed this")]
    model = ScriptedModel(scripts=[[AIMessageChunk(content="partial"), failure], second_request])
    chunks: list[AIMessageChunk] = []

    with pytest.raises(ClientError) as caught:
        await stream_llm(
            model,
            [SystemMessage(content="stable head"), HumanMessage(content="conversation")],
            chunks=chunks,
            handler=StubHandler(),
            agent=AgentSlices.resolve(default_reader=defaults()),
            catalog=model_catalog,
            monotonic=time.monotonic,
        )

    assert caught.value is failure
    assert [chunk.content for chunk in chunks] == ["partial"]
    assert model.scripts == [second_request]


async def test_stream_binds_the_agent_tool_and_sends_the_complete_prefix(
    model_catalog: ModelCatalog,
) -> None:
    chunks: list[AIMessageChunk] = []

    await stream_llm(
        ScriptedModel(scripts=[["echo"]]),
        [SystemMessage(content="stable head"), HumanMessage(content="conversation")],
        chunks=chunks,
        handler=StubHandler(),
        agent=AgentSlices.resolve(default_reader=defaults()),
        catalog=model_catalog,
        monotonic=time.monotonic,
    )

    assert chunks[0].content == f"{(execute_code.name,)}: stable head | conversation"
