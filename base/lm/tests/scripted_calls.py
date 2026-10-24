# meiva:note
# Scripted doubles of the two model calls a caller is handed as ports.
#
# `ScriptedDecide` satisfies `base.lm.decision.call.DecideCall` and `ScriptedDraftModel`
# satisfies `base.lm.call.DraftModel` / `DraftModelFactory`. Each answers from its script and
# records what it was asked; setting `error` makes the next call raise instead.

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from base.lm.call import StructuredReply
from base.lm.catalog import ModelCatalog
from base.lm.catalog.decide.types import Decision, Question
from base.lm.catalog.provider_keys import ProviderKeys

__all__ = ["ScriptedDecide", "ScriptedDraftModel"]


# meiva:note
# Answers every decision call with `result`; `calls` holds (model, state, questions, kwargs).
class ScriptedDecide:
    def __init__(self, result: Mapping[str, Decision]) -> None:
        self.result = result
        self.error: Exception | None = None
        self.calls: list[tuple[str, str, Mapping[str, Question], dict[str, Any]]] = []

    async def __call__(
        self, model: str, state: str, questions: Mapping[str, Question], **kwargs: Any
    ) -> Mapping[str, Decision]:
        self.calls.append((model, state, questions, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


# meiva:note
# A draft model that answers `reply` (parsed as JSON for the structured result).
#
# It is its own factory and its own bound structured model: `built` holds the model ids it was
# asked to build, `bindings` the schema and options of each `with_structured_output`,
# `invocations` the message lists it answered. `usage` and `metadata` shape the raw message.
class ScriptedDraftModel:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.error: Exception | None = None
        self.built: list[str] = []
        self.invocations: list[Sequence[BaseMessage]] = []
        self.bindings: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.usage = {"input_tokens": 120, "output_tokens": 14, "total_tokens": 134}
        self.raw = AIMessage(content=reply)
        self.metadata = {"model_provider": "anthropic", "stop_reason": "end_turn"}

    def factory(self, model: str, catalog: ModelCatalog, keys: ProviderKeys) -> ScriptedDraftModel:
        del catalog, keys
        self.built.append(model)
        return self

    def with_structured_output(
        self, schema: dict[str, Any], *, method: str, strict: bool, include_raw: bool
    ) -> ScriptedDraftModel:
        self.bindings.append(
            (schema, {"method": method, "strict": strict, "include_raw": include_raw})
        )
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> StructuredReply:
        self.invocations.append(messages)
        if self.error is not None:
            raise self.error
        self.raw = AIMessage(
            content=self.reply, response_metadata=self.metadata, usage_metadata=self.usage
        )
        try:
            parsed = json.loads(self.reply)
        except json.JSONDecodeError as error:
            return {"raw": self.raw, "parsed": None, "parsing_error": error}
        return {"raw": self.raw, "parsed": parsed, "parsing_error": None}
