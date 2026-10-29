# meiva:link meiva_builtins/plugins/lm_typesafe/docs/systemone/systemone.meiva.okf.md

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel

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
from base.log import logger
from base.packages.plugins.extensions import PluginContributions

from . import systemone
from .local_provider import local_contribution

_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
_MODEL = "jev-1.13.0"


class JevBinding:
    """One Jev request answers all questions about the state."""

    def __init__(self, ctx: DecisionBuildContext, api_key: str) -> None:
        self._ctx = ctx
        self._api_key = api_key

    async def decide(self, state: str, questions: Mapping[str, Question]) -> DecisionResult:
        payload = {
            "state": state,
            "model": self._ctx.model,
            "questions": {qid: systemone.wire_question(q) for qid, q in questions.items()},
        }
        body = await post_json(self._ctx, url=_ENDPOINT, api_key=self._api_key, payload=payload)
        tokens = systemone.input_tokens(body)
        answered = body.get("model")
        if answered != _MODEL:
            logger.warning(
                "Jev answered with model {answered!r}, not the pinned {pinned!r}",
                answered=answered,
                pinned=_MODEL,
            )
        try:
            return DecisionResult(systemone.parse_answers(body, questions), tokens)
        except DecisionResponseError as exc:
            raise DecisionResponseError(str(exc), input_tokens=tokens) from exc


def build(ctx: DecisionBuildContext) -> DecisionBinding:
    return JevBinding(ctx, ctx.keys.require("TYPESAFE_API_KEY"))


PROVIDER = DecisionProviderContribution(
    prefix="jev-",
    display_name="TypeSafe",
    key_env="TYPESAFE_API_KEY",
    build=build,
    models={
        # 32k bounds the state plus the longest single question, 64k the state plus all
        # questions; text only (https://docs.typesafe.ai/models).
        _MODEL: DecisionModelSpec(context_window=32_000, max_request_tokens=64_000),
    },
    pricing={
        # $0.042 per million input tokens, output free, no cache discount.
        _MODEL: PriceRates(
            cache_miss=0.042,
            cache_hit=0.042,
            output=0.0,
            source_url="https://docs.typesafe.ai/models",
            source_checked_at="2026-10-11",
            vendor="typesafe",
        ),
    },
)


def configure(config: BaseModel) -> PluginContributions:
    return PluginContributions(decision_providers=(PROVIDER, local_contribution(config)))
