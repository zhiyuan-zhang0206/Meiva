# meiva:note
# A chat model that plays a script for one agent: the running steps of a real hosted turn,
# each of which can write code for the agent to run, say its reply, or hold until its test
# lets it go. Reached through the ordinary `llm_override` factory seam (`module:build`), so
# the turn under test runs the production graph, claim node and settlement around it.

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGenerationChunk, ChatResult

OVERRIDE = f"{__name__}:build"

_USAGE: UsageMetadata = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}


# meiva:note
# One model call: write `code` for the agent to run, or say `text`. A call that `holds` waits
# for the script's `release` before it finishes, after `partial` (if any) has streamed.
@dataclass(frozen=True)
class Step:
    text: str = "done"
    code: str | None = None
    partial: str | None = None
    holds: bool = False


# meiva:note
# One agent's model calls. `entered` is set on each call. A call past the end of the script
# says "done". `histories` records what each call was shown.
@dataclass
class Script:
    steps: list[Step] = field(default_factory=lambda: [Step(holds=True)])
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    histories: list[list[BaseMessage]] = field(default_factory=list)

    @property
    def calls(self) -> int:
        return len(self.histories)


SCRIPTS: dict[int, Script] = {}


class ScriptedChatModel(BaseChatModel):
    agent_id: int

    @property
    def _llm_type(self) -> str:
        return "scripted-test"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:
        del tools, kwargs
        return self

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        raise NotImplementedError("the hosted graph streams the model asynchronously")

    async def _astream(
        self, messages: list[BaseMessage], *args: Any, **kwargs: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        del args, kwargs
        script = SCRIPTS[self.agent_id]
        step = script.steps[min(len(script.histories), len(script.steps) - 1)]
        script.histories.append(list(messages))
        script.entered.set()
        if step.partial is not None:
            yield ChatGenerationChunk(message=AIMessageChunk(content=step.partial))
        if step.holds:
            await script.release.wait()
        if step.code is not None:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    response_metadata={"model_provider": "anthropic", "stop_reason": "tool_use"},
                    usage_metadata=_USAGE,
                    tool_call_chunks=[
                        {
                            "name": "execute_code",
                            "args": json.dumps({"code": step.code}),
                            "id": f"call_{len(script.histories)}",
                            "index": 0,
                        }
                    ],
                )
            )
            return
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content=step.text,
                response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
                usage_metadata=_USAGE,
            )
        )


def build(model: str, *, agent_id: int | None) -> ScriptedChatModel:
    del model
    if agent_id is None:
        raise ValueError("the scripted test model serves one agent's turn")
    return ScriptedChatModel(agent_id=agent_id)
