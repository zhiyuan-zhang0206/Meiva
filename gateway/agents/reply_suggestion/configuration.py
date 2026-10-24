from __future__ import annotations

from base.config import ConfigBoot
from gateway.agents.reply_suggestion.plan import ReplySuggestionPolicy

__all__ = ["reply_suggestion_policy"]


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
