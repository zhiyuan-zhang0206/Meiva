"""One streaming model request under stage and total deadlines.

Provider transport failures are normalized at the call boundary. Stall,
protocol and permanent failures propagate without a fallback request.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, AnyMessage

from agent.graph._callbacks import RedisStreamHandler
from agent.graph.llm_errors import (
    LLMStreamStallTimeoutError,
)
from agent.llm import execute_code
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.errors import normalize_provider_transport_error
from base.lm.registry import resolve_setting

from ._deadlines import (
    StreamBounds,
    decode_window_ms,
    milliseconds_between,
    next_chunk_wait,
    stage_seconds,
    total_spent,
)


class _ModelWaitTimeoutError(TimeoutError):
    """The model await exceeded its owned deadline; unrelated timeouts do not."""


async def _await_model_message(operation: Awaitable[AIMessage], timeout: float) -> AIMessage:
    deadline = asyncio.timeout(timeout)
    try:
        async with deadline:
            return await operation
    except TimeoutError as exc:
        if deadline.expired():
            raise _ModelWaitTimeoutError(f"Model wait exceeded {timeout:g}s") from exc
        raise
    except Exception as exc:
        normalized = normalize_provider_transport_error(exc)
        if normalized is exc:
            raise
        raise normalized from exc


# meiva:note
# The deadlines of an agent's next streaming attempt: its own pins, else its model's tuning.
def stream_bounds(agent: AgentSlices, catalog: ModelCatalog) -> StreamBounds:
    model = agent.brain.llm_model
    return StreamBounds(
        ttft_seconds=resolve_setting(
            "llm_stream_ttft_timeout_seconds",
            model=model,
            models=catalog.models,
            explicit=agent.overrides.llm_stream_ttft_timeout_seconds,
        ),
        inter_chunk_seconds=resolve_setting(
            "llm_stream_inter_chunk_timeout_seconds",
            model=model,
            models=catalog.models,
            explicit=agent.overrides.llm_stream_inter_chunk_timeout_seconds,
        ),
        total_seconds=resolve_setting(
            "llm_stream_total_timeout_seconds",
            model=model,
            models=catalog.models,
            explicit=agent.overrides.llm_stream_total_timeout_seconds,
        ),
    )


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
                    handler=handler,
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


# meiva:note
# An owned `asyncio.timeout` deadline wraps only the model's `__anext__`. Expiry raises `LLMStreamStallTimeoutError`; an ordinary model or callback `TimeoutError` keeps its identity without claiming that timer expired. `bounds.ttft_seconds` bounds the first chunk (slower models may have a long cold start), `bounds.inter_chunk_seconds` every later one, `bounds.total_seconds` the whole attempt even when every gap stays healthy. `StopAsyncIteration` is not wrapped in a timeout (an empty stream must not wait), and `chunk_idx` separates the TTFT stall from the mid-stream one for ops. Returns the monotonic arrival times of the first and last chunk, the stream's decode window; an empty stream returns `(None, None)`, and a stall propagates no timestamps because the attempt is discarded.
# meiva:link agent/graph/llm/docs/llm.meiva.okf.md
async def consume_stream_with_stall_timeout(
    stream_iter: AsyncIterator[AIMessage],
    *,
    chunks: list[AIMessageChunk],
    handler: RedisStreamHandler,
    bounds: StreamBounds,
) -> tuple[float | None, float | None]:
    chunk_idx = 0
    first_ts: float | None = None
    last_ts: float | None = None
    started_at = time.monotonic()
    while True:
        wait = next_chunk_wait(bounds, chunk_idx, time.monotonic() - started_at)
        if wait is None:
            raise _total_stall(bounds, chunk_idx)
        try:
            chunk = await _await_model_message(stream_iter.__anext__(), wait.seconds)
        except StopAsyncIteration:
            if total_spent(bounds, time.monotonic() - started_at):
                raise _total_stall(bounds, chunk_idx) from None
            return (first_ts, last_ts)
        except _ModelWaitTimeoutError as e:
            if wait.total_is_bound:
                raise _total_stall(bounds, chunk_idx) from e
            raise _stage_stall(stage_seconds(bounds, chunk_idx), chunk_idx) from e
        if not isinstance(chunk, AIMessageChunk):
            raise TypeError(f"the model stream yielded {type(chunk).__name__}, not a chunk")
        # Arrival timestamps before fan-out: decode_ms measures the provider's
        # generation window (first token → last token), excluding the
        # synchronous SSE publish cost on our side.
        now = time.monotonic()
        if total_spent(bounds, now - started_at):
            raise _total_stall(bounds, chunk_idx)
        if first_ts is None:
            first_ts = now
        last_ts = now
        chunks.append(chunk)
        chunk_idx += 1
        handler.process_chunk(chunk, now)


def _stage_stall(stage_timeout: float, chunk_idx: int) -> LLMStreamStallTimeoutError:
    stage_label = "TTFT" if chunk_idx == 0 else f"mid-stream after {chunk_idx} chunks"
    return LLMStreamStallTimeoutError(
        f"LLM stream stalled — no chunk for {stage_timeout:.1f}s ({stage_label}); "
        f"abort turn. Provider hang / network drop suspected.",
        stage="ttft" if chunk_idx == 0 else "mid-stream",
    )


def _total_stall(bounds: StreamBounds, chunk_idx: int) -> LLMStreamStallTimeoutError:
    total = bounds.total_seconds
    if total is None:
        raise RuntimeError("the total ceiling was reported spent but the stream has none")
    return LLMStreamStallTimeoutError(
        f"LLM stream exceeded {total:.1f}s total duration "
        f"after {chunk_idx} chunks; abort streaming attempt.",
        stage="total",
    )


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
