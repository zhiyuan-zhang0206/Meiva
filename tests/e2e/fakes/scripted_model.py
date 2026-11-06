from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.messages.tool import ToolCallChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import PrivateAttr

from base.agents.messages.reply_intent import DISPLAY_TO_USER, HIDE_FROM_USER

__all__ = ["ScriptExhaustedError", "ScriptedFakeChatModel", "user_facing", "working_note"]


class ScriptExhaustedError(RuntimeError):
    pass


# meiva:note
# A script answers like a compliant model. Every non-empty assistant text block must open with
# an explicit visibility line; text without one is folded into Details and answered with a
# correction note, which spends a model call the script does not contain. `user_facing` is an
# answer the user reads in the main conversation; `working_note` is text that belongs in Details.
# meiva:link base/agents/messages/docs/metadata-contracts.meiva.okf.md
def user_facing(text: str) -> str:
    return f"{DISPLAY_TO_USER}\n{text}"


def working_note(text: str) -> str:
    return f"{HIDE_FROM_USER}\n{text}"


class ScriptedFakeChatModel(BaseChatModel):
    script: tuple[AIMessage, ...]
    _cursor: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedFakeChatModel:
        return self

    @property
    def cursor(self) -> int:
        return self._cursor

    def _next_message(self) -> AIMessage:
        if self._cursor >= len(self.script):
            raise ScriptExhaustedError(
                f"ScriptedFakeChatModel: script exhausted at turn {self._cursor} "
                f"(script length {len(self.script)}) -- scenario did not cover turn {self._cursor + 1}"
            )
        msg = self.script[self._cursor]
        self._cursor += 1
        return msg

    def _make_chunk(self, msg: AIMessage) -> ChatGenerationChunk:
        tool_call_chunks: list[ToolCallChunk] = [
            {
                "name": tc["name"],
                "args": json.dumps(tc["args"]),
                "id": tc.get("id"),
                "index": idx,
            }
            for idx, tc in enumerate(msg.tool_calls)
        ]
        response_metadata = msg.response_metadata or {
            "model_provider": "anthropic",
            "stop_reason": "tool_use" if msg.tool_calls else "end_turn",
        }
        chunk_msg = AIMessageChunk(
            content=msg.content,
            tool_call_chunks=tool_call_chunks,
            usage_metadata=msg.usage_metadata,
            response_metadata=response_metadata,
        )
        return ChatGenerationChunk(message=chunk_msg)

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        msg = self._next_message()
        yield self._make_chunk(msg)

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ):
        msg = self._next_message()
        yield self._make_chunk(msg)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        msg = self._next_message()
        return ChatResult(generations=[ChatGeneration(message=msg)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        msg = self._next_message()
        return ChatResult(generations=[ChatGeneration(message=msg)])
