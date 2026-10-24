# meiva:note
# What a start does about the home's stand-down record, asserted on the returned step.

from __future__ import annotations

import pytest

from cli.commands.lifecycle.start.stand_down_gate import StandDownStep, stand_down_step

_WINDOW = "declared window ends in 90s"


def _step(*, boot: bool, recorded: bool = True, passed: bool = False) -> StandDownStep:
    return stand_down_step(
        boot_attempt=boot, recorded=recorded, deadline_passed=passed, window=_WINDOW
    )


def test_the_boot_job_leaves_a_stopped_home_down_and_ends_successfully() -> None:
    step = _step(boot=True)

    assert step == StandDownStep(
        line=f"stand-down ({_WINDOW}): the boot job leaves this home stopped",
        clear=False,
        skip=True,
    )


def test_the_boot_job_never_clears_the_record_it_obeys() -> None:
    assert _step(boot=True).clear is False


@pytest.mark.parametrize(
    ("recorded", "passed"),
    [(False, False), (True, True)],
    ids=["no record", "declared window ended"],
)
def test_the_boot_job_proceeds_when_nothing_holds_the_home(recorded: bool, passed: bool) -> None:
    assert _step(boot=True, recorded=recorded, passed=passed) == StandDownStep(
        line=None, clear=False, skip=False
    )


def test_an_explicit_start_ends_a_stand_down_and_says_so() -> None:
    assert _step(boot=False) == StandDownStep(
        line="stand-down cleared by an explicit start", clear=True, skip=False
    )


def test_an_explicit_start_clears_even_a_record_whose_window_ended() -> None:
    assert _step(boot=False, passed=True).clear is True


def test_an_explicit_start_with_no_record_reports_nothing() -> None:
    assert _step(boot=False, recorded=False) == StandDownStep(line=None, clear=False, skip=False)
