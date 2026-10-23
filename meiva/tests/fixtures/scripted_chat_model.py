# meiva:note
# A chat model a test scripts, handed to the real model factory through its documented
# `llm_override` seam. A test writes `OVERRIDE` into the configuration value the code under test
# reads (`llm_override`), scripts the model it names with `scripted_models.script(model, ...)`,
# and runs the real code: the factory resolves the override to a fresh `ScriptedChatModel` that
# answers from the script, so nothing is patched. The script keeps the requests it received; a
# model nobody scripted is refused. A reply is a text, an exception that is raised when its turn
# comes (how a provider failure is scripted), a ready message, or a function of the request. `refuse` makes the
# factory itself fail for a model (how a missing API key is scripted). The last reply
# repeats once the script runs out.
# meiva:link meiva/tests/fixtures/docs/sdk-composition.meiva.okf.md

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from typing import Annotated, Any

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, SkipValidation

__all__ = [
    "OVERRIDE",
    "Script",
    "ScriptedChatModel",
    "ScriptedModels",
    "scripted",
    "scripted_models",
]

OVERRIDE = "meiva.tests.fixtures.scripted_chat_model:scripted"

type Reply = str | AIMessage | Exception | Callable[[list[BaseMessage]], str]


class Script:
    def __init__(self, replies: tuple[Reply, ...]) -> None:
        if not replies:
            raise ValueError("a script needs at least one reply")
        self.replies = replies
        self.requests: list[list[BaseMessage]] = []
        self._lock = threading.Lock()

    def take(self, messages: list[BaseMessage]) -> Reply:
        with self._lock:
            self.requests.append(list(messages))
            return self.replies[min(len(self.requests), len(self.replies)) - 1]

    def texts(self, request: int = -1) -> list[str]:
        texts: list[str] = []
        for message in self.requests[request]:
            content = message.content
            blocks: list[Any] = content if isinstance(content, list) else [content]
            texts.extend(
                block if isinstance(block, str) else block["text"]
                for block in blocks
                if isinstance(block, str) or block.get("type") == "text"
            )
        return texts


class ScriptedChatModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    script: Annotated[Script, SkipValidation]

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        reply = self.script.take(messages)
        if isinstance(reply, Exception):
            raise reply
        answer = reply(messages) if callable(reply) else reply
        message = answer if isinstance(answer, AIMessage) else AIMessage(content=answer)
        return ChatResult(generations=[ChatGeneration(message=message)])


class ScriptedModels:
    def __init__(self) -> None:
        self._scripts: dict[str, Script] = {}
        self._refused: list[str] = []

    def script(self, model: str, *replies: Reply) -> Script:
        script = Script(replies)
        self.bind(model, script)
        return script

    def bind(self, model: str, script: Script) -> None:
        _SCRIPTS[model] = script
        self._scripts[model] = script

    def refuse(self, model: str, error: Exception) -> None:
        _REFUSALS[model] = error
        self._refused.append(model)

    def clear(self) -> None:
        for model in self._scripts:
            _SCRIPTS.pop(model, None)
        self._scripts.clear()
        for model in self._refused:
            _REFUSALS.pop(model, None)
        self._refused.clear()


_SCRIPTS: dict[str, Script] = {}
_REFUSALS: dict[str, Exception] = {}


def scripted(model: str, *, agent_id: int | None = None) -> BaseChatModel:
    refusal = _REFUSALS.get(model)
    if refusal is not None:
        raise refusal
    script = _SCRIPTS.get(model)
    if script is None:
        raise AssertionError(f"no script for model {model!r}: the test did not plan this call")
    return ScriptedChatModel(script=script)


@pytest.fixture
def scripted_models() -> Iterator[ScriptedModels]:
    models = ScriptedModels()
    try:
        yield models
    finally:
        models.clear()
