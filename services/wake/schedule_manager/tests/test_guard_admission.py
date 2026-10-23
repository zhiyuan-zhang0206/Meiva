from __future__ import annotations

import pytest

from services.wake.schedule_manager.guard_admission import (
    Admission,
    Frame,
    ParkFiles,
    admit_stall,
    close_failure_is_fatal,
    is_parked,
    stop_admission,
)

FILES = ParkFiles(subprocess="/lib/subprocess.py", selectors="/lib/selectors.py")


def test_a_stall_is_admitted_while_the_guard_is_open() -> None:
    state, admitted = admit_stall(Admission())

    assert admitted
    assert state == Admission(stopped=False, stalled=True)


def test_closing_after_a_stall_reports_the_admitted_action() -> None:
    claimed, _ = admit_stall(Admission())

    state, action_started = stop_admission(claimed)

    assert action_started
    assert state == Admission(stopped=True, stalled=True)


# meiva:note
# The sample was in progress when the script finished: close wins and nothing runs.
def test_a_stall_sampled_before_close_cannot_be_claimed_after_it() -> None:
    closed, action_started = stop_admission(Admission())

    state, admitted = admit_stall(closed)

    assert not action_started
    assert not admitted
    assert state == Admission(stopped=True, stalled=False)


def test_a_claimed_stall_is_not_taken_back_by_a_second_claim_or_a_close() -> None:
    claimed, _ = admit_stall(Admission())
    again, admitted_again = admit_stall(claimed)
    closed, action_started = stop_admission(again)

    assert admitted_again
    assert action_started
    assert closed.stalled


@pytest.mark.parametrize("function", ["sleep", "wait", "wait_for", "run_forever", "acquire"])
def test_sleep_family_frames_park(function: str) -> None:
    assert is_parked(Frame("/anywhere.py", function, None, None), FILES)


def test_ordinary_work_does_not_park() -> None:
    assert not is_parked(Frame("/script.py", "fetch", "/script.py", "main"), FILES)


def test_the_subprocess_wait_parks() -> None:
    assert is_parked(Frame(FILES.subprocess, "_wait", None, None), FILES)
    assert not is_parked(Frame(FILES.subprocess, "_execute_child", None, None), FILES)
    assert not is_parked(Frame("/other/subprocess.py", "_wait", None, None), FILES)


def test_the_selector_parks_only_inside_communicate() -> None:
    inside = Frame(FILES.selectors, "select", FILES.subprocess, "_communicate")
    outside = Frame(FILES.selectors, "select", "/script.py", "main")
    in_conversion = Frame(FILES.selectors, "select", FILES.subprocess, "__fspath__")

    assert is_parked(inside, FILES)
    assert not is_parked(outside, FILES)
    assert not is_parked(in_conversion, FILES)


@pytest.mark.parametrize(
    "primary",
    [None, SystemExit(0), SystemExit(None), SystemExit(False)],
    ids=["no-failure", "exit-0", "exit-none", "exit-false"],
)
def test_a_guard_close_failure_is_raised_when_the_script_ended_cleanly(
    primary: BaseException | None,
) -> None:
    assert close_failure_is_fatal(primary)


@pytest.mark.parametrize(
    "primary",
    [ValueError("script failed"), SystemExit(3), SystemExit(True), SystemExit("failed")],
    ids=["exception", "exit-3", "exit-true", "exit-message"],
)
def test_a_guard_close_failure_is_logged_when_the_script_failed(primary: BaseException) -> None:
    assert not close_failure_is_fatal(primary)
