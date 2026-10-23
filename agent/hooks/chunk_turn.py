# meiva:note
# What an llm turn and a compaction mean for understanding chunks, decided from values: nothing, the segment's baseline, or a chunk to enqueue with the state that follows it. The shell in understanding_chunks.py reads the config, enqueues, and writes the state; the trigger rule itself is base's `plan_chunk`.
# meiva:link agent/hooks/docs/hooks.meiva.okf.md
from __future__ import annotations

from dataclasses import dataclass

from langchain_core.messages import AIMessage, AnyMessage

from agent.state_channels import CompactState
from base.agents.history.hierarchy.chunks import (
    Chunk,
    plan_chunk,
    plan_closing_chunk,
    segment_head_len,
    sendable_len,
)


# meiva:note
# A segment's first turn: the cut moves past the head and its tokens become the growth reference, so the fixed head never counts as material.
@dataclass(frozen=True)
class Baseline:
    state: CompactState


# meiva:note
# A chunk to enqueue, the id of its last message (a chunk whose last message has no id cannot be located later), and the state once it is on the queue.
@dataclass(frozen=True)
class EnqueueChunk:
    chunk: Chunk
    end_msg_id: str | None
    moved: CompactState


TurnPlan = Baseline | EnqueueChunk | None


# meiva:note
# The closing segment's remainder at compaction: from the last cut to the end, up to the last sendable message.
@dataclass(frozen=True)
class ClosingChunk:
    chunk: Chunk
    end_msg_id: str | None


# meiva:note
# `request` is the message list the turn sent (head included); `final_msg` carries the provider's usage for it. None means nothing to do this turn.
def plan_turn(
    compact: CompactState, request: list[AnyMessage], final_msg: AIMessage, *, threshold: int
) -> TurnPlan:
    usage = final_msg.usage_metadata
    input_tokens = int(usage["input_tokens"]) if usage else 0
    if input_tokens == 0:
        return None
    if compact.understanding_cut_tokens == 0:
        return Baseline(
            compact.model_copy(
                update={
                    "understanding_cut_index": max(
                        compact.understanding_cut_index, segment_head_len(request)
                    ),
                    "understanding_cut_tokens": input_tokens,
                }
            )
        )
    chunk = plan_chunk(
        cut_index=compact.understanding_cut_index,
        cut_tokens=compact.understanding_cut_tokens,
        input_tokens=input_tokens,
        request_len=len(request),
        threshold=threshold,
    )
    if chunk is None:
        return None
    return EnqueueChunk(
        chunk,
        request[chunk.end_index - 1].id,
        compact.model_copy(
            update={
                "understanding_cut_index": chunk.end_index,
                "understanding_cut_tokens": input_tokens,
            }
        ),
    )


def plan_closing(compact: CompactState, messages: list[AnyMessage]) -> ClosingChunk | None:
    chunk = plan_closing_chunk(
        cut_index=max(compact.understanding_cut_index, segment_head_len(messages)),
        request_len=sendable_len(messages),
    )
    if chunk is None:
        return None
    return ClosingChunk(chunk, messages[chunk.end_index - 1].id)
