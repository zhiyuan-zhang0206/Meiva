# meiva:note
# Chat-model clients for compaction tests. The model is the provider boundary, so the test hands
# the code a client that answers from a script and refuses what the script did not plan: a reply
# past the end of the script raises, and a generation nobody scripted is an assertion failure.
# What the code did with the replies is read from its result, not from the client.

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import NoReturn

from langchain_core.messages import AIMessage, AIMessageChunk


class ScriptedModel:
    def __init__(
        self, *replies: AIMessage | BaseException, generation: AIMessageChunk | None = None
    ) -> None:
        self._replies = list(replies)
        self._generation = generation

    def bind_tools(self, *_args: object, **_kwargs: object) -> ScriptedModel:
        return self

    async def ainvoke(self, *_args: object, **_kwargs: object) -> AIMessage:
        reply = self._replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    async def astream(self, *_args: object, **_kwargs: object) -> AsyncIterator[AIMessageChunk]:
        if self._generation is None:
            raise AssertionError("an ordinary generation nobody scripted")
        yield self._generation


class HangingSummarizer:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.settled = asyncio.Event()

    def bind_tools(self, *_args: object, **_kwargs: object) -> HangingSummarizer:
        return self

    async def ainvoke(self, *_args: object, **_kwargs: object) -> AIMessage:
        self.started.set()
        try:
            await asyncio.Future()
        finally:
            self.settled.set()
        raise AssertionError("unreachable")

    def astream(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("an interrupted compaction must not start an ordinary generation")
