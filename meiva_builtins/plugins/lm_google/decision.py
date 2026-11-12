# meiva:note
# Gemini Decisions API: one text input and typed questions, no generated output.
#
# Wire schema verified against Google's python-genai decisions resource and a synthetic
# live request. API keys use x-goog-api-key, never an OAuth bearer or URL query.

from __future__ import annotations

from collections.abc import Mapping

from base.lm.catalog.decide.types import DecisionInputTooLargeError
from base.lm.provider_api import (
    DecisionBinding,
    DecisionBuildContext,
    DecisionModelSpec,
    DecisionProviderContribution,
    DecisionResponseError,
    DecisionResult,
    PriceRates,
    Question,
    post_json,
)

from . import decision_protocol

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/decisions"
MODEL = "gemini-decision-1"


class GeminiDecisionBinding:
    # meiva:note
    # An authenticated, single-request decision client.

    def __init__(self, ctx: DecisionBuildContext) -> None:
        self._ctx = ctx
        self._key = ctx.keys.require("GEMINI_API_KEY")

    async def decide(self, state: str, questions: Mapping[str, Question]) -> DecisionResult:
        if len(questions) > 1000:
            raise DecisionInputTooLargeError(
                "Gemini Decisions supports at most 1000 questions per request"
            )
        body = await post_json(
            self._ctx,
            url=ENDPOINT,
            api_key=None,
            headers={"x-goog-api-key": self._key},
            payload={
                "model": MODEL,
                "input": [{"type": "text", "text": state}],
                "questions": {
                    qid: decision_protocol.wire_question(q) for qid, q in questions.items()
                },
            },
        )
        tokens = decision_protocol.input_tokens(body)
        try:
            return DecisionResult(decision_protocol.parse_answers(body, questions), tokens)
        except DecisionResponseError as exc:
            raise DecisionResponseError(str(exc), input_tokens=tokens) from exc


def build(ctx: DecisionBuildContext) -> DecisionBinding:
    # meiva:note
    # Build without contacting the provider; require its existing Gemini key.
    return GeminiDecisionBinding(ctx)


DECISION_PROVIDER = DecisionProviderContribution(
    prefix="gemini-",
    display_name="Google",
    key_env="GEMINI_API_KEY",
    build=build,
    models={
        MODEL: DecisionModelSpec(
            context_window=256_000, media_types=frozenset({"image", "audio", "video", "pdf"})
        )
    },
    pricing={
        MODEL: PriceRates(
            cache_miss=0.10,
            cache_hit=0.10,
            output=0.0,
            source_url="https://blog.google/innovation-and-ai/technology/developers-tools/gemini-decisions-api/",
            source_checked_at="2026-11-12",
            vendor="google",
        )
    },
)
