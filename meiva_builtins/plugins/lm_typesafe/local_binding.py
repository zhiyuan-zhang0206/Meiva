# meiva:link meiva_builtins/plugins/lm_typesafe/docs/systemone/systemone.meiva.okf.md

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TypeGuard

from base.lm.provider_api import (
    Choice,
    DecisionBuildContext,
    DecisionRequestError,
    DecisionResponseError,
    DecisionResult,
    Question,
    Score,
    post_json,
)

from . import systemone
from .default_config import LocalDecisionModel


def _is_object(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


class LocalDecisionBinding:
    def __init__(
        self, ctx: DecisionBuildContext, *, endpoint: str, registration: LocalDecisionModel
    ) -> None:
        self._ctx = ctx
        self._endpoint = endpoint
        self._registration = registration

    async def decide(self, state: str, questions: Mapping[str, Question]) -> DecisionResult:
        wire = {qid: systemone.wire_question(q) for qid, q in questions.items()}
        self._check_capabilities(questions, wire)
        payload = {
            "model": self._registration.wire_model,
            "state": state,
            "questions": wire,
        }
        try:
            async with asyncio.timeout(self._ctx.timeout):
                body = await post_json(
                    self._ctx,
                    url=self._endpoint,
                    api_key=self._ctx.keys.secrets.get("LOCAL_DECISION_API_KEY"),
                    payload=payload,
                )
        except TimeoutError as exc:
            raise DecisionRequestError(
                f"{self._ctx.model}: local decision deadline exceeded"
            ) from exc
        tokens = systemone.input_tokens(body)
        try:
            self._check_receipt(body)
            return DecisionResult(systemone.parse_answers(body, questions), tokens)
        except DecisionResponseError as exc:
            raise DecisionResponseError(str(exc), input_tokens=tokens) from exc

    def _check_capabilities(
        self, questions: Mapping[str, Question], wire: Mapping[str, Mapping[str, object]]
    ) -> None:
        for qid, question in questions.items():
            if wire[qid]["type"] not in self._registration.question_types:
                raise ValueError(f"{self._ctx.model}: unsupported question type for {qid!r}")
            if (
                isinstance(question, Choice)
                and len(question.options) > self._registration.max_choice_options
            ):
                raise ValueError(f"{self._ctx.model}: too many choice options for {qid!r}")
            if (
                isinstance(question, Score)
                and len(question.scale) > self._registration.max_score_levels
            ):
                raise ValueError(f"{self._ctx.model}: too many score levels for {qid!r}")

    def _check_receipt(self, body: Mapping[str, object]) -> None:
        if body.get("model") != self._registration.response_model:
            raise DecisionResponseError(
                f"{self._ctx.model}: unexpected response model {body.get('model')!r}"
            )
        routing = body.get("routing")
        if routing is not None and (
            not _is_object(routing) or routing.get("model") != self._registration.wire_model
        ):
            raise DecisionResponseError(f"{self._ctx.model}: service used a different model route")
        usage = body["usage"]
        if not _is_object(usage):
            raise DecisionResponseError("local decision usage is not an object")
        if (
            usage.get("truncated")
            or usage.get("state_tokens_dropped")
            or usage.get("truncated_questions")
            or usage.get("options")
        ):
            raise DecisionResponseError(
                f"{self._ctx.model}: service truncated input or collapsed options"
            )
