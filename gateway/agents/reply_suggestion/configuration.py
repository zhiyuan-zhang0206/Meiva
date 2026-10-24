from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from base.config import ConfigBoot
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from base.lm.decision.call import decide
from base.lm.effort import ReasoningEffort
from base.lm.factory import build_chat_model
from gateway.agents.reply_suggestion.plan import ReplySuggestionPolicy
from gateway.agents.reply_suggestion.service import (
    DRAFT_TIMEOUT_S,
    ReplySuggestionService,
)

__all__ = ["draft_model_options", "reply_suggestion_policy", "reply_suggestion_service"]


# meiva:note
# The suggestion settings as `config` holds them now.
def reply_suggestion_policy(config: ConfigBoot) -> ReplySuggestionPolicy:
    lm = config.view.lm
    return ReplySuggestionPolicy(
        enabled=lm.reply_suggestion_enabled,
        model=lm.reply_suggestion_model,
        gate_model=lm.reply_suggestion_gate_model,
        gate_threshold=lm.reply_suggestion_gate_threshold,
        max_input_tokens=lm.reply_suggestion_max_input_tokens,
        max_candidates=lm.reply_suggestion_max_candidates,
    )


# meiva:note
# How the drafting model is built: reasoning off (DeepSeek defaults to maximum effort,
# which makes a one-line draft run for tens of seconds), a bounded wait, and no metering of
# its own (the service logs the usage).
def draft_model_options(llm_override: str) -> dict[str, Any]:
    return {
        "reasoning_effort": ReasoningEffort.NONE,
        "timeout": DRAFT_TIMEOUT_S,
        "meter": False,
        "llm_override": llm_override,
        "overrides": ModelOverrides.from_pins({}),
    }


def reply_suggestion_service(llm_override: str) -> ReplySuggestionService:
    def draft_model(model: str, catalog: ModelCatalog, keys: ProviderKeys) -> BaseChatModel:
        return build_chat_model(
            model, catalog=catalog, keys=keys, **draft_model_options(llm_override)
        )

    return ReplySuggestionService(decide=decide, draft_model=draft_model)
