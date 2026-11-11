# meiva:note
# One streaming model request under stage and total deadlines. The turn's llm node and the compaction summary call both stream through here, so a model that keeps emitting is never cut by a wall-clock total, and a real stall fails by the same rule in both. Provider transport failures are normalized at the call boundary. Stall, protocol and permanent failures propagate without a fallback request.
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, AnyMessage

from agent.graph.llm_errors import LLMStreamStallTimeoutError
from agent.graph.llm_stream.chunk import assemble_final_message
from agent.graph.llm_stream.deadlines import (
    StreamBounds,
    next_chunk_wait,
    stage_seconds,
    total_spent,
)
from agent.llm import execute_code
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.errors import normalize_provider_transport_error
from base.lm.registry import resolve_setting

__all__ = [
    "consume_stream_with_stall_timeout",
    "stream_bounds",
    "stream_message",
]


# meiva:note
# The model await exceeded its owned deadline; unrelated timeouts do not.
class _ModelWaitTimeoutError(TimeoutError):
    pass


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


# meiva:note
# An owned `asyncio.timeout` deadline wraps only the model's `__anext__`. Expiry raises `LLMStreamStallTimeoutError`; an ordinary model or callback `TimeoutError` keeps its identity without claiming that timer expired. `bounds.ttft_seconds` bounds the first chunk (slower models may have a long cold start), `bounds.inter_chunk_seconds` every later one, `bounds.total_seconds` the whole attempt even when every gap stays healthy. `StopAsyncIteration` is not wrapped in a timeout (an empty stream must not wait), and `chunk_idx` separates the TTFT stall from the mid-stream one for ops. Each chunk is appended to `chunks` and handed to `on_chunk` with its monotonic arrival time, taken before the fan-out so the decode window excludes the publish cost on our side. Returns the monotonic arrival times of the first and last chunk, the stream's decode window; an empty stream returns `(None, None)`, and a stall propagates no timestamps because the attempt is discarded.
# meiva:link agent/graph/llm/docs/llm.md
async def consume_stream_with_stall_timeout(
    stream_iter: AsyncIterator[AIMessage],
    *,
    chunks: list[AIMessageChunk],
    on_chunk: Callable[[AIMessageChunk, float], None],
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
        now = time.monotonic()
        if total_spent(bounds, now - started_at):
            raise _total_stall(bounds, chunk_idx)
        if first_ts is None:
            first_ts = now
        last_ts = now
        chunks.append(chunk)
        chunk_idx += 1
        on_chunk(chunk, now)


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


# meiva:note
# One streamed call whose chunks are folded into the complete `AIMessage` and validated like a turn's. A call outside the turn loop (the compaction summary) has no live fan-out: it streams for the same stall rules, so a model that keeps emitting is not cut by a wall-clock total. An empty stream returns an empty message.
# meiva:link agent/graph/llm/docs/llm.md
async def stream_message(
    llm: BaseChatModel,
    messages: list[AnyMessage],
    *,
    agent: AgentSlices,
    catalog: ModelCatalog,
) -> AIMessage:
    runnable = llm.bind_tools([execute_code])
    chunks: list[AIMessageChunk] = []
    await consume_stream_with_stall_timeout(
        runnable.astream(messages).__aiter__(),
        chunks=chunks,
        on_chunk=lambda _chunk, _now: None,
        bounds=stream_bounds(agent, catalog),
    )
    if not chunks:
        return AIMessage(content="")
    return assemble_final_message(chunks, catalog=catalog)
