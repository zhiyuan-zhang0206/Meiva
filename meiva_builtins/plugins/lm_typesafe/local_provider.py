# meiva:link meiva_builtins/plugins/lm_typesafe/docs/systemone/systemone.meiva.okf.md

from __future__ import annotations

from functools import partial

from pydantic import BaseModel

from base.lm.provider_api import (
    DecisionBinding,
    DecisionBuildContext,
    DecisionModelSpec,
    DecisionProviderContribution,
    PriceRates,
)

from .default_config import LocalDecisionConfig
from .local_binding import LocalDecisionBinding


def _build(ctx: DecisionBuildContext, *, config: LocalDecisionConfig) -> DecisionBinding:
    registration = config.local_models[ctx.model]
    return LocalDecisionBinding(ctx, endpoint=config.local_endpoint, registration=registration)


def local_contribution(config: BaseModel) -> DecisionProviderContribution:
    snapshot = LocalDecisionConfig.model_validate(config.model_dump(mode="json"))
    return DecisionProviderContribution(
        prefix="local-decision-",
        display_name="Local decision service",
        key_env="LOCAL_DECISION_API_KEY",
        build=partial(_build, config=snapshot),
        models={
            model_id: DecisionModelSpec(
                context_window=row.context_window,
                max_request_tokens=row.max_request_tokens,
            )
            for model_id, row in snapshot.local_models.items()
        },
        pricing={
            model_id: PriceRates(
                cache_miss=0.0,
                cache_hit=0.0,
                output=0.0,
                source_url="https://github.com/NandhaKishorM/laya/blob/v0.4.17/laya/serve.py",
                source_checked_at="2026-10-29",
                vendor="local",
            )
            for model_id in snapshot.local_models
        },
        requires_key=False,
    )
