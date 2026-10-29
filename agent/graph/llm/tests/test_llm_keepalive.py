import asyncio
from collections.abc import AsyncIterator

import pytest
from langchain_core.messages import AIMessageChunk, AnyMessage

from agent.graph._callbacks import RedisStreamHandler
from agent.graph.llm._stream import consume_llm
from agent.graph.llm.tests.llm_support import end_turn_chunk
from agent.graph.llm_errors import LLMStreamStallTimeoutError
from base.agents.observation.turn_progress import TurnProgress
from base.config.tests.configuration import defaults
from base.events.live.tests.live_capture import LiveEvents
from base.events.live.tests.live_capture import live_events as live_events
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog


class _QuietModel:
    def __init__(self, released: asyncio.Event, fails: bool, empty_summary: bool) -> None:
        self.released = released
        self.fails = fails
        self.empty_summary = empty_summary

    async def astream(self, _messages: list[AnyMessage]) -> AsyncIterator[AIMessageChunk]:
        if self.empty_summary:
            yield AIMessageChunk(content=[{"type": "reasoning", "summary": []}])
        await self.released.wait()
        if self.fails:
            raise RuntimeError("provider failed")
        yield end_turn_chunk()


@pytest.mark.parametrize("empty_summary", [False, True])
@pytest.mark.parametrize("end", ["success", "failure", "stall", "cancel"])
async def test_silent_request_keeps_ui_alive_and_stops_with_its_owner(
    end: str, empty_summary: bool, live_events: LiveEvents, model_catalog: ModelCatalog
) -> None:
    progress = TurnProgress()
    handler = RedisStreamHandler(live_events.publisher, 7, 5, progress)
    agent = AgentSlices.resolve(
        default_reader=defaults(
            lm__llm_stream_ttft_timeout_seconds=1.5 if end == "stall" else 10.0,
            lm__llm_stream_inter_chunk_timeout_seconds=1.5 if end == "stall" else 10.0,
        )
    )
    released = asyncio.Event()
    chunks: list[AIMessageChunk] = []
    task = asyncio.create_task(
        consume_llm(
            _QuietModel(released, end == "failure", empty_summary),
            [],
            chunks=chunks,
            handler=handler,
            agent=agent,
            catalog=model_catalog,
        )
    )
    try:
        await asyncio.sleep(0.55)
        events = await live_events.events()
        assert len(events) >= 2
        assert all(
            event == {"role": "llm_keepalive", "agent_id": 7, "msg_idx": 5, "content": ""}
            for event in events
        )
        assert len(chunks) == int(empty_summary)
        marks = progress.snapshot(7)
        assert (marks is not None) == empty_summary
        if marks is not None:
            assert len(marks["last_marks"]) == 1
        await _finish_request(task, released, end, empty_summary)
        handler.flush_deltas()
        if end == "success":
            assert any(chunk.content == "ok" for chunk in chunks)
        await live_events.events()
        await asyncio.sleep(0.55)
        assert await live_events.events() == []
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _finish_request(
    task: asyncio.Task[tuple[float | None, float | None]],
    released: asyncio.Event,
    end: str,
    empty_summary: bool,
) -> None:
    if end == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif end == "failure":
        released.set()
        with pytest.raises(RuntimeError, match="provider failed"):
            await task
    elif end == "stall":
        with pytest.raises(
            LLMStreamStallTimeoutError, match="mid-stream" if empty_summary else "TTFT"
        ):
            await task
    else:
        released.set()
        await task
