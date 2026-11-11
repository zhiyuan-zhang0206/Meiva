# meiva:note
# A chat model that replays scripted replies and keeps what it was asked: the requests it
# received, in order, and the tools bound to it. The last reply repeats once the script runs out,
# so a model that always answers needs one reply. An exception in the script is raised when its turn
# comes, which is how a provider failure is scripted. A test asserts the requests and the outcome
# instead of counting calls on a mock.

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import Annotated, Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import ConfigDict, Field, SkipValidation


class ScriptedChatModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    replies: Annotated[list[AIMessage | Exception], SkipValidation]
    requests: list[list[BaseMessage]] = Field(default_factory=list[list[BaseMessage]])
    bound_tools: list[list[Any]] = Field(default_factory=list[list[Any]])

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], *, tool_choice: str | None = None, **kwargs: Any):  # pyright: ignore[reportIncompatibleMethodOverride]
        # meiva:note
        # The model binds nothing: it remembers the tools and answers as itself, so the request
        # a caller sends after binding is the one recorded.
        self.bound_tools.append(list(tools))
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        reply = self._next_reply(messages)
        return ChatResult(generations=[ChatGeneration(message=reply)])

    # meiva:note
    # The streamed form of the same scripted reply: one chunk that ends like a provider's last frame (a normal terminal reason and usage, unless the reply carries its own), so a caller that streams and validates the stream gets the reply it scripted.
    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        reply = self._next_reply(messages)
        calls = reply.tool_calls
        yield ChatGenerationChunk(
            message=AIMessageChunk(
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
        )

    def _next_reply(self, messages: list[BaseMessage]) -> AIMessage:
        self.requests.append(list(messages))
        reply = self.replies[min(len(self.requests), len(self.replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply
