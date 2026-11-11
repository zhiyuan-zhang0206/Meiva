# meiva:note
# The llm node's streaming request: live fan-out over the shared stall rules. The deadlines and the chunk consumer are `agent/graph/llm_stream/stream.py`; this shell adds the turn's keepalive heartbeat and stamps latency and decode timing.
from __future__ import annotations

import asyncio
from collections.abc import Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, AnyMessage

from agent.graph._callbacks import RedisStreamHandler
from agent.graph.llm_stream.deadlines import decode_window_ms, milliseconds_between
from agent.graph.llm_stream.stream import consume_stream_with_stall_timeout, stream_bounds
from agent.llm import execute_code
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog


async def consume_llm(
    bound_llm: object,
    messages: list[AnyMessage],
    *,
    chunks: list[AIMessageChunk],
    handler: RedisStreamHandler,
    agent: AgentSlices,
    catalog: ModelCatalog,
) -> tuple[float | None, float | None]:
    """Stream once under owned deadlines; any failure propagates to the node."""
    try:
        async with asyncio.TaskGroup() as tasks:
            heartbeat = tasks.create_task(_publish_keepalives(handler))
            try:
                return await consume_stream_with_stall_timeout(
                    bound_llm.astream(messages).__aiter__(),  # type: ignore[attr-defined]
                    chunks=chunks,
                    on_chunk=handler.process_chunk,
                    bounds=stream_bounds(agent, catalog),
                )
            finally:
                heartbeat.cancel()
    # meiva:note
    # Keep a single provider failure typed for the node retry classifier; real sibling failures stay grouped.
    except ExceptionGroup as group:
        if len(group.exceptions) == 1:
            raise group.exceptions[0] from None
        raise


async def _publish_keepalives(handler: RedisStreamHandler) -> None:
    while True:
        handler.keepalive()
        await asyncio.sleep(0.5)


async def stream_llm(
    llm: BaseChatModel,
    messages: list[AnyMessage],
    *,
    chunks: list[AIMessageChunk],
    handler: RedisStreamHandler,
    agent: AgentSlices,
    catalog: ModelCatalog,
    monotonic: Callable[[], float],
) -> None:
    """Bind tools, stream the complete prefix and stamp latency/decode timing.

    Keep the existing SystemMessage byte-stable for provider implicit caching.
    Empty streams have no decode window.
    """
    call_started = monotonic()
    runnable = llm.bind_tools([execute_code])
    first_ts, last_ts = await consume_llm(
        runnable, messages, chunks=chunks, handler=handler, agent=agent, catalog=catalog
    )
    handler.llm_latency_ms = milliseconds_between(call_started, monotonic())
    handler.llm_decode_ms = decode_window_ms(first_ts, last_ts)
