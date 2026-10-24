# meiva:note
# What a finished `meiva start` owes the boot unit and the boot scheduler.

from __future__ import annotations

import pytest

from cli.startup.outcome import (
    BootDone,
    BootGaveUp,
    BootRetry,
    HandOffRoot,
    plan_after_start,
    plan_boot_settlement,
)


def test_a_complete_success_hands_the_root_to_the_boot_unit() -> None:
    assert plan_after_start(0) == HandOffRoot()


@pytest.mark.parametrize("result", [1, 2, 75])
def test_a_start_that_left_anything_unready_hands_nothing_over(result: int) -> None:
    assert plan_after_start(result) is None


def test_a_successful_attempt_is_done_whatever_its_number() -> None:
    assert plan_boot_settlement(0, attempt=1, max_attempts=10, spent=False) == BootDone()
    assert plan_boot_settlement(0, attempt=11, max_attempts=10, spent=True) == BootDone()


@pytest.mark.parametrize("code", [1, 75])
def test_a_failed_attempt_with_budget_left_keeps_the_starts_own_exit_code(code: int) -> None:
    assert plan_boot_settlement(code, attempt=3, max_attempts=10, spent=False) == BootRetry(code)


def test_the_failed_attempt_that_spends_the_budget_gives_up_and_says_why() -> None:
    plan = plan_boot_settlement(75, attempt=10, max_attempts=10, spent=True)

    assert plan == BootGaveUp(
        attempt=10,
        failed_code=75,
        message=(
            "meiva start --boot: attempt 10 of 10 failed (exit 75); "
            "not retrying until an operator runs `meiva start`"
        ),
    )
