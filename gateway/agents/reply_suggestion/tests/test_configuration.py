from __future__ import annotations

import pytest
from pydantic import ValidationError

from base.config.domains.lm import LmSettings
from base.config.tests.configuration import snapshot_process_config
from base.lm.effort import ReasoningEffort
from gateway.agents.reply_suggestion.configuration import (
    draft_model_options,
    reply_suggestion_policy,
)
from gateway.agents.reply_suggestion.service import DRAFT_TIMEOUT_S


def test_defaults() -> None:
    lm = LmSettings()

    assert lm.reply_suggestion_enabled is True
    assert lm.reply_suggestion_model == "deepseek-flash"
    assert lm.reply_suggestion_gate_model == "jev-1.13.0"
    assert lm.reply_suggestion_gate_threshold == 0.5
    assert lm.reply_suggestion_max_input_tokens == 1500
    assert lm.reply_suggestion_max_candidates == 3


@pytest.mark.parametrize("threshold", [-0.1, 1.1])
def test_the_threshold_must_be_a_probability(threshold: float) -> None:
    with pytest.raises(ValidationError):
        LmSettings.model_validate({"MEIVA_REPLY_SUGGESTION_GATE_THRESHOLD": threshold})


def test_the_input_budget_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        LmSettings.model_validate({"MEIVA_REPLY_SUGGESTION_MAX_INPUT_TOKENS": 0})


def test_the_policy_is_read_from_the_config_owner_it_is_given() -> None:
    first = snapshot_process_config()
    first.set_field("reply_suggestion_gate_threshold", 0.7)
    first.set_field("reply_suggestion_max_candidates", 2)
    second = snapshot_process_config()
    second.set_field("reply_suggestion_enabled", False)

    one = reply_suggestion_policy(first)
    other = reply_suggestion_policy(second)

    assert (one.gate_threshold, one.max_candidates, one.enabled) == (0.7, 2, True)
    assert other.enabled is False
    assert other.max_candidates == 3


def test_the_draft_model_is_built_with_reasoning_off_and_a_bounded_wait() -> None:
    options = draft_model_options("override-model")

    assert options["reasoning_effort"] is ReasoningEffort.NONE
    assert options["timeout"] == DRAFT_TIMEOUT_S
    assert options["meter"] is False
    assert options["llm_override"] == "override-model"
