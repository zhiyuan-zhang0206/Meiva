# meiva:note
# Chat models for the labeler's tests, reached through the product's own `llm_override` seam.
#
# `base.lm.factory.build_chat_model` hands the requested model name to the override factory when
# `llm_override` is set, so a test selects a model's behaviour by naming it. The name is
# `<scenario>:<argument>`; every scenario is a fixed rule below, nothing is configured per test.
#
# - `say:<text>`     the model replies with `<text>`
# - `blocks:<text>`  the model replies with a thinking block (carrying a signature) and a text block
# - `fail:<name>`    the call raises the exception `ERRORS[<name>]` builds
# - `build:<name>`   building the model raises the exception `ERRORS[<name>]` builds
# - `hang:`         the call never returns (a test cancels it)
# - `usage:<text>`   the reply carries token usage for a model the catalog does not price
# - `failif:<needle>:<name>`  the call raises `ERRORS[<name>]` when the user request contains
#                    `<needle>`, and replies with a label otherwise
# - any name without a colon: a reply that carries token usage but no service receipt, which the
#                    usage accounting rejects for a catalog model that is billed by speed
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import httpx2
import openai
from langchain_core.exceptions import ModelConnectionError
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

SCRIPTED_MODELS = "services.derived.labeler.tests.scripted_models:factory"


def _status_error(status: int) -> openai.APIStatusError:
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://audit.invalid"))
    return openai.APIStatusError(f"HTTP {status}", response=response, body=None)


class _StatusLookalikeError(RuntimeError):
    status_code = 429


# meiva:note
# The exceptions a scenario can raise, by name. The first six are trusted transient provider
# failures; the rest are failures the labeler must not mistake for one.
ERRORS: dict[str, Callable[[], BaseException]] = {
    "sdk-connection": lambda: openai.APIConnectionError(
        request=httpx2.Request("POST", "https://audit.invalid")
    ),
    "rate-limit": lambda: _status_error(429),
    "temporary-server": lambda: _status_error(503),
    "model-connection": lambda: ModelConnectionError("provider connection lost"),
    "http-timeout": lambda: httpx.ReadTimeout("provider timed out"),
    "http-connection": lambda: httpx.ConnectError("provider connection lost"),
    "type-error": lambda: TypeError("invalid invocation"),
    "network-prose": lambda: RuntimeError("network timed out"),
    "status-lookalike": lambda: _StatusLookalikeError("rate limited"),
    "auth": lambda: _status_error(401),
    "invalid-request": lambda: _status_error(400),
}

TRANSIENT = (
    "sdk-connection",
    "rate-limit",
    "temporary-server",
    "model-connection",
    "http-timeout",
    "http-connection",
)
PERMANENT = ("type-error", "network-prose", "status-lookalike", "auth", "invalid-request")


class _Scripted(BaseChatModel):
    reply: Any = ""
    error_name: str | None = None
    usage: bool = False

    @property
    def _llm_type(self) -> str:
        return "labeler-scripted"

    failing_needle: str | None = None
    hangs: bool = False

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        if self.error_name is not None and (
            self.failing_needle is None
            or any(self.failing_needle in str(message.content) for message in messages)
        ):
            error = ERRORS[self.error_name]()
            raise error
        message = AIMessage(content=self.reply)
        if self.usage:
            message.usage_metadata = {"input_tokens": 12, "output_tokens": 4, "total_tokens": 16}
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        if self.hangs:
            await asyncio.Event().wait()
        return self._generate(messages, stop, run_manager, **kwargs)


def factory(model: str, *, agent_id: int | None = None) -> BaseChatModel:
    scenario, separator, argument = model.partition(":")
    if not separator:
        return _Scripted(reply="Fix label generation", usage=True)
    if scenario == "say":
        return _Scripted(reply=argument)
    if scenario == "blocks":
        return _Scripted(
            reply=[
                {"type": "thinking", "thinking": "weighing the request", "signature": "SIG-LEAK"},
                {"type": "text", "text": argument},
            ]
        )
    if scenario == "fail":
        return _Scripted(error_name=argument)
    if scenario == "hang":
        return _Scripted(hangs=True)
    if scenario == "build":
        error = ERRORS[argument]()
        raise error
    if scenario == "usage":
        return _Scripted(reply=argument, usage=True)
    if scenario == "failif":
        needle, _, name = argument.rpartition(":")
        return _Scripted(reply="Fix label generation", error_name=name, failing_needle=needle)
    raise ValueError(f"unknown labeler test scenario in model name {model!r}")
