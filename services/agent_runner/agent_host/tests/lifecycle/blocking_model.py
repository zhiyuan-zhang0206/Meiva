# meiva:note
# A chat model whose reply waits for its test: the running step of a real hosted turn.
# Reached through the ordinary `llm_override` factory seam (`module:build`), so the turn
# under test runs the production graph, claim node and settlement around it.

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult

OVERRIDE = f"{__name__}:build"


# meiva:note
# One agent's model calls: `entered` is set on each call, which returns once `release` is.
@dataclass
class Gate:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    calls: int = 0


GATES: dict[int, Gate] = {}


class BlockingChatModel(BaseChatModel):
    agent_id: int

    @property
    def _llm_type(self) -> str:
        return "blocking-test"

    def bind_tools(self, tools: Any, **kwargs: Any) -> BlockingChatModel:
        del tools, kwargs
        return self

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        raise NotImplementedError("the hosted graph streams the model asynchronously")

    async def _astream(
        self, messages: list[BaseMessage], *args: Any, **kwargs: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        del messages, args, kwargs
        gate = GATES[self.agent_id]
        gate.calls += 1
        gate.entered.set()
        await gate.release.wait()
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="done",
                response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )
        )


def build(model: str, *, agent_id: int | None) -> BlockingChatModel:
    del model
    if agent_id is None:
        raise ValueError("the blocking test model serves one agent's turn")
    return BlockingChatModel(agent_id=agent_id)
