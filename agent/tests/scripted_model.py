# meiva:note
# A chat model that replays scripted replies and keeps what it was asked: the requests it
# received, in order, and the tools bound to it. The last reply repeats once the script runs out,
# so a model that always answers needs one reply. An exception in the script is raised when its turn
# comes, which is how a provider failure is scripted. A test asserts the requests and the outcome
# instead of counting calls on a mock.

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
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
        self.requests.append(list(messages))
        reply = self.replies[min(len(self.requests), len(self.replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return ChatResult(generations=[ChatGeneration(message=reply)])
