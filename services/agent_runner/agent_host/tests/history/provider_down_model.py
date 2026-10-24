# meiva:note
# A chat model whose provider is down: every call raises the error registered for its agent
# and is counted. Reached through the ordinary `llm_override` factory seam (`module:build`), so
# the compaction under test runs the production hook, retry loop and abort around it.

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult

OVERRIDE = f"{__name__}:build"

FAILURES: dict[int, BaseException] = {}
CALLS: dict[int, int] = {}


class ProviderDownModel(BaseChatModel):
    agent_id: int

    @property
    def _llm_type(self) -> str:
        return "provider-down-test"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ProviderDownModel:
        del tools, kwargs
        return self

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        raise NotImplementedError("the hosted graph calls the model asynchronously")

    async def _agenerate(
        self, messages: list[BaseMessage], *args: Any, **kwargs: Any
    ) -> ChatResult:
        del messages, args, kwargs
        CALLS[self.agent_id] = CALLS.get(self.agent_id, 0) + 1
        raise FAILURES[self.agent_id]


def build(model: str, *, agent_id: int | None) -> ProviderDownModel:
    del model
    if agent_id is None:
        raise ValueError("the provider-down test model serves one agent's turn")
    return ProviderDownModel(agent_id=agent_id)
