# meiva:note
# Chat-model clients for compaction tests. The model is the provider boundary, so the test hands
# the code a client that answers from a script and refuses what the script did not plan: a reply
# past the end of the script raises, and a generation nobody scripted is an assertion failure.
# A compaction summary is a streamed call whose last message is the compaction checkpoint; the
# client tells it from an ordinary generation by that message and plays the next scripted reply
# as one closing chunk, the way a provider ends a stream. What the code did with the replies is
# read from its result, not from the client.

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage

_CHECKPOINT_MARK = "Context compaction checkpoint"


def asks_for_summary(messages: Sequence[BaseMessage]) -> bool:
    return bool(messages) and _CHECKPOINT_MARK in str(messages[-1].content)


def closing_chunk(reply: AIMessage) -> AIMessageChunk:
    calls = reply.tool_calls
    return AIMessageChunk(
        content=reply.content,
        id=reply.id,
        tool_call_chunks=[
            {
                "name": call["name"],
                "args": json.dumps(call["args"]),
                "id": call["id"],
                "index": index,
                "type": "tool_call_chunk",
            }
            for index, call in enumerate(calls)
        ],
        response_metadata={
            "model_provider": "anthropic",
            "stop_reason": "tool_use" if calls else "end_turn",
            **reply.response_metadata,
        },
        usage_metadata=reply.usage_metadata
        or {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )


class ScriptedModel:
    def __init__(
        self, *replies: AIMessage | BaseException, generation: AIMessageChunk | None = None
    ) -> None:
        self._replies = list(replies)
        self._generation = generation

    def bind_tools(self, *_args: object, **_kwargs: object) -> ScriptedModel:
        return self

    async def astream(
        self, messages: Sequence[BaseMessage], *_args: object, **_kwargs: object
    ) -> AsyncIterator[AIMessageChunk]:
        if asks_for_summary(messages):
            reply = self._replies.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            yield closing_chunk(reply)
            return
        if self._generation is None:
            raise AssertionError("an ordinary generation nobody scripted")
        yield self._generation


class HangingSummarizer:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.settled = asyncio.Event()

    def bind_tools(self, *_args: object, **_kwargs: object) -> HangingSummarizer:
        return self

    async def astream(
        self, messages: Sequence[BaseMessage], *_args: object, **_kwargs: object
    ) -> AsyncIterator[AIMessageChunk]:
        if not asks_for_summary(messages):
            raise AssertionError("an interrupted compaction must not start an ordinary generation")
        self.started.set()
        try:
            await asyncio.Future()
        finally:
            self.settled.set()
        yield AIMessageChunk(content="")
