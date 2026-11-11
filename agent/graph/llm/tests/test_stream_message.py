# meiva:note
# A streamed call outside the turn loop (the compaction summary) is bounded by stalls, not by a wall-clock total.

from __future__ import annotations

import time
from typing import cast
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage

from agent.graph._callbacks import RedisStreamHandler
from agent.graph.llm._stream import stream_llm
from agent.graph.llm.tests.llm_support import ScriptedModel, end_turn_chunk, sink_publisher
from agent.graph.llm_errors import LLMStreamStallTimeoutError, LLMStreamTruncatedError
from agent.graph.llm_stream.stream import stream_message
from base.agents.observation.turn_progress import TurnProgress
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog

_STAGE_SECONDS = 0.3
_GAP_SECONDS = 0.1


def _agent() -> AgentSlices:
    return AgentSlices.resolve(
        default_reader=defaults(
            lm__llm_stream_ttft_timeout_seconds=_STAGE_SECONDS,
            lm__llm_stream_inter_chunk_timeout_seconds=_STAGE_SECONDS,
        )
    )


# meiva:note
# Twenty chunks over twice the stage bound in all: every gap is healthy, so the call is not cut, and the chunks are folded into the one message the caller reads.
async def test_a_summary_that_keeps_arriving_outlasts_the_stage_bounds(
    model_catalog: ModelCatalog,
) -> None:
    drip = [step for i in range(20) for step in (_GAP_SECONDS, AIMessageChunk(content=f"{i} "))]
    model = ScriptedModel(scripts=[[*drip, end_turn_chunk("done")]])

    message = await stream_message(
        model, [HumanMessage(content="history")], agent=_agent(), catalog=model_catalog
    )

    assert message.text == "".join(f"{i} " for i in range(20)) + "done"
    assert message.usage_metadata is not None


async def test_a_summary_whose_first_token_never_comes_fails_as_a_stall(
    model_catalog: ModelCatalog,
) -> None:
    model = ScriptedModel(scripts=[["hang"]])

    with pytest.raises(LLMStreamStallTimeoutError, match="TTFT"):
        await stream_message(
            model, [HumanMessage(content="history")], agent=_agent(), catalog=model_catalog
        )


async def test_a_summary_that_stops_midway_fails_as_a_stall(model_catalog: ModelCatalog) -> None:
    model = ScriptedModel(
        scripts=[[AIMessageChunk(content="a"), AIMessageChunk(content="b"), "hang"]]
    )

    with pytest.raises(LLMStreamStallTimeoutError, match="mid-stream after 2 chunks"):
        await stream_message(
            model, [HumanMessage(content="history")], agent=_agent(), catalog=model_catalog
        )


# meiva:note
# A summary cut off by the output limit is rejected by the same terminal-reason validation a turn applies, not folded into the history as if it were complete.
async def test_a_summary_cut_off_by_the_output_limit_is_a_failure(
    model_catalog: ModelCatalog,
) -> None:
    cut_off = AIMessageChunk(
        content="half a summ",
        response_metadata={"model_provider": "anthropic", "stop_reason": "max_tokens"},
        usage_metadata={"input_tokens": 1, "output_tokens": 4, "total_tokens": 5},
    )
    model = ScriptedModel(scripts=[[cut_off]])

    with pytest.raises(LLMStreamTruncatedError):
        await stream_message(
            model, [HumanMessage(content="history")], agent=_agent(), catalog=model_catalog
        )


# meiva:note
# The node's real wiring: `stream_llm` hands every chunk to its handler's fan-out through the shared consumer, so the text a turn streams reaches the frontend publisher.
async def test_stream_llm_delivers_chunks_to_the_handler_fan_out(
    model_catalog: ModelCatalog,
) -> None:
    publisher = sink_publisher()
    handler = RedisStreamHandler(
        event_publisher=publisher, agent_id=1, msg_idx=0, turn_progress=TurnProgress()
    )
    model = ScriptedModel(scripts=[[AIMessageChunk(content="streamed text"), end_turn_chunk("!")]])
    chunks: list[AIMessageChunk] = []

    await stream_llm(
        model,
        [HumanMessage(content="history")],
        chunks=chunks,
        handler=handler,
        agent=_agent(),
        catalog=model_catalog,
        monotonic=time.monotonic,
    )
    handler.flush_deltas()

    emitted = str(cast("MagicMock", publisher.emit).call_args_list)
    assert "streamed text" in emitted
