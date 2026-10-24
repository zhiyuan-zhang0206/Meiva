from __future__ import annotations

from typing import cast

import pytest

from base.config import ConfigBoot, Settings
from base.config.domains.lm import LmSettings
from base.host.env.agent_slices import ModelOverrides
from services.derived.labeler import daemon


def test_the_slice_carries_the_value_each_boot_owner_holds() -> None:
    first = ConfigBoot()
    first.read_process_environment()
    second = ConfigBoot()
    second.read_process_environment()
    first.set_field("labeler_model", "label-model-x")
    first.set_field("labeler_max_chars", 33)
    second.set_field("labeler_model", "label-model-y")
    second.set_field("labeler_max_chars", 44)

    a = daemon.labeler_config(first)
    b = daemon.labeler_config(second)

    assert (a.labeler_model, a.labeler_max_chars) == ("label-model-x", 33)
    assert (b.labeler_model, b.labeler_max_chars) == ("label-model-y", 44)
    assert a.poll_interval_s == daemon.POLL_INTERVAL_S


def test_gateway_tuning_does_not_read_stripped_agent_fields() -> None:
    runtime = Settings(profile="gateway")

    class _PoisonLm:
        @property
        def reasoning_effort(self) -> str:
            raise AssertionError("gateway must not read stripped reasoning effort")

        @property
        def claude_thinking_budget_tokens(self) -> int:
            raise AssertionError("gateway must not read stripped thinking budget")

    assert runtime.profile == "gateway"
    actual = daemon.labeler_model_overrides(
        profile=runtime.profile, lm=cast(LmSettings, _PoisonLm())
    )
    assert actual == ModelOverrides.from_pins({})


@pytest.mark.parametrize("effort,budget", [(None, None), ("high", 2048), ("", 0)])
def test_full_tuning_keeps_explicit_values(effort: str | None, budget: int | None) -> None:
    lm = LmSettings.model_validate(
        {"reasoning_effort": effort, "claude_thinking_budget_tokens": budget}
    )
    runtime = Settings(profile=None, lm=lm)
    actual = daemon.labeler_model_overrides(profile=None, lm=runtime.lm)
    assert actual.reasoning_effort == effort
    assert actual.claude_thinking_budget_tokens == budget


def test_labeler_tuning_refuses_unknown_profile() -> None:
    with pytest.raises(ValueError, match="not a known process profile"):
        daemon.labeler_model_overrides(profile="unknown", lm=LmSettings())
