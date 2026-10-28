# meiva:note
# The held path's settle budget is configuration, not a bare literal. This module owns the field's
# own contract: its default, its positivity, and the one bound its own domain can see — the turn's
# no-progress bound, the only neighbour that lives in this domain. The deploy family's two bounds
# belong to the clock lattice, which this domain may not import (that edge would close a cycle);
# `base/deploy/tests/test_timing_topology.py` composes the configured value with it.
# meiva:link docs/decisions/runtime/config/2026-10-28-held-settle-budget-configurable.md

from __future__ import annotations

import pytest
from pydantic import ValidationError

from base.config.domains.daemon.settings import DaemonSettings


def test_the_default_is_the_five_seconds_the_decision_names() -> None:
    assert DaemonSettings().held_settle_budget_s == 5.0


def test_the_field_declares_that_default() -> None:
    assert DaemonSettings.model_fields["held_settle_budget_s"].default == 5.0


@pytest.mark.parametrize("budget", [0.5, 4.5, 60.0])
def test_a_positive_budget_is_accepted(budget: float) -> None:
    assert DaemonSettings(MEIVA_HOST_HELD_SETTLE_BUDGET_S=budget).held_settle_budget_s == budget


# meiva:note
# The turn bound is configurable too, so "the budget is small" is not by itself enough: a runner
# whose turn bound sits below its settle budget would spend a budget it never gets back.
def test_a_budget_that_reaches_the_turns_own_bound_refuses_to_load() -> None:
    with pytest.raises(ValidationError) as refusal:
        DaemonSettings.model_validate(
            {
                "MEIVA_HOST_HELD_SETTLE_BUDGET_S": 8.0,
                "MEIVA_HOST_TURN_NO_PROGRESS_TIMEOUT_SECONDS": 8.0,
            }
        )
    assert "MEIVA_HOST_TURN_NO_PROGRESS_TIMEOUT_SECONDS=8s" in str(refusal.value)


def test_a_budget_of_zero_or_less_is_refused() -> None:
    with pytest.raises(ValidationError):
        DaemonSettings.model_validate({"MEIVA_HOST_HELD_SETTLE_BUDGET_S": 0})
