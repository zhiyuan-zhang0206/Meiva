from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import httpx
import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from agent.llm import execute_code
from agent.llm.invoke import ainvoke_tool_call
from base.lm.errors import is_retryable_provider_error


# meiva:note
# A chat model that answers with what it was shown, after `delay` seconds. `failures` are raised one per call, oldest first, before any answer; what is left in it afterwards is how many scripted failures the caller never reached.
class _Model(BaseChatModel):
    failures: list[BaseException] = Field(default_factory=list)
    delay: float = 0.0
    tools: tuple[str, ...] = ()

    @property
    def _llm_type(self) -> str:
        return "invoke-test"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> _Model:
        del kwargs
        return self.model_copy(update={"tools": tuple(tool.name for tool in tools)})

    def _generate(self, *args: Any, **kwargs: Any) -> ChatResult:
        raise NotImplementedError("the compaction call is asynchronous")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        if self.failures:
            raise self.failures.pop(0)
        await asyncio.sleep(self.delay)
        shown = " | ".join(str(message.content) for message in messages)
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=f"{self.tools}: {shown}"))]
        )


_HISTORY: list[AnyMessage] = [
    SystemMessage(content="stable head"),
    HumanMessage(content="history"),
]


async def test_tool_call_preserves_complete_prefix() -> None:
    response = await ainvoke_tool_call(_Model(), _HISTORY, timeout_seconds=30.0)

    assert response.content == f"{(execute_code.name,)}: stable head | history"


async def test_compaction_deadline_cancels_model() -> None:
    with pytest.raises(TimeoutError):
        await ainvoke_tool_call(_Model(delay=30.0), _HISTORY, timeout_seconds=0.01)


async def test_the_deadline_is_the_value_each_call_passes() -> None:
    model = _Model(delay=0.05)

    with pytest.raises(TimeoutError):
        await ainvoke_tool_call(model, _HISTORY, timeout_seconds=0.002)
    response = await ainvoke_tool_call(model, _HISTORY, timeout_seconds=30.0)

    assert response.content == f"{(execute_code.name,)}: stable head | history"


async def test_unknown_failure_propagates_once() -> None:
    failure = TypeError("model code failed")
    unreached = TypeError("a second call would have reached this")
    model = _Model(failures=[failure, unreached])

    with pytest.raises(TypeError) as caught:
        await ainvoke_tool_call(model, _HISTORY, timeout_seconds=30.0)

    assert caught.value is failure
    assert model.failures == [unreached]


async def test_cancellation_reaches_the_caller_while_the_model_is_in_flight() -> None:
    call = asyncio.create_task(
        ainvoke_tool_call(_Model(delay=30.0), _HISTORY, timeout_seconds=60.0)
    )
    await asyncio.sleep(0.01)
    call.cancel()

    with pytest.raises(asyncio.CancelledError):
        await call


async def test_transport_error_is_normalized_for_caller_retry() -> None:
    failure = httpx.ConnectError("wire lost")
    unreached = httpx.ConnectError("a second call would have reached this")
    model = _Model(failures=[failure, unreached])

    with pytest.raises(Exception) as caught:
        await ainvoke_tool_call(model, _HISTORY, timeout_seconds=30.0)

    assert caught.value.__cause__ is failure
    assert is_retryable_provider_error(caught.value)
    assert model.failures == [unreached]
