from __future__ import annotations

from typing import cast

import pytest

from gateway.schedules.plans import (
    ControlAction,
    capture_window,
    changed_fields,
    control_plan,
    edit_needs_sync,
    logs_view,
)


def _row(
    *,
    name: str = "daily",
    description: str | None = None,
    command: str = "python schedule.py",
    enabled: bool = True,
    script: str = "print(1)",
) -> tuple[object, ...]:
    # meiva:note
    # id, name, description, command, enabled, status, last_error, created_at, updated_at, script
    return (1, name, description, command, enabled, "stopped", None, None, None, script)


def test_only_fields_that_differ_from_the_row_are_changes() -> None:
    previous = _row()

    assert changed_fields(previous, {"name": "daily", "script": "print(2)"}) == {
        "script": "print(2)"
    }
    assert changed_fields(previous, {"enabled": True, "command": "python schedule.py"}) == {}


def test_unknown_fields_are_ignored() -> None:
    assert changed_fields(_row(), {"nope": 1, "description": "hi"}) == {"description": "hi"}


@pytest.mark.parametrize(
    ("changed", "enabled", "expected"),
    [
        ({"script": "x"}, True, True),
        ({"command": "x"}, True, True),
        ({"script": "x"}, False, False),
        ({"enabled": False}, False, True),
        ({"enabled": True}, True, True),
        ({"name": "x", "description": "y"}, True, False),
        ({}, True, False),
    ],
)
def test_an_edit_converges_the_session_now_only_for_new_code_on_an_enabled_schedule_or_a_flip(
    changed: dict[str, object], enabled: bool, expected: bool
) -> None:
    assert edit_needs_sync(changed, enabled=enabled) is expected


@pytest.mark.parametrize("status", ["stopped", "running", "completed", "error"])
def test_start_of_a_disabled_schedule_enables_and_syncs(status: str) -> None:
    plan = control_plan("start", enabled=False, status=status)

    assert (plan.refusal, plan.enabled, plan.needs_sync) == (None, True, True)


@pytest.mark.parametrize(
    ("status", "needs_sync"),
    [("stopped", False), ("running", False), ("completed", True), ("error", True)],
)
def test_start_of_an_enabled_schedule_syncs_only_when_it_has_ended(
    status: str, needs_sync: bool
) -> None:
    plan = control_plan("start", enabled=True, status=status)

    assert (plan.refusal, plan.enabled, plan.needs_sync) == (None, True, needs_sync)


def test_stop_disables_and_syncs_an_enabled_schedule_but_not_a_stopped_one() -> None:
    assert control_plan("stop", enabled=True, status="running").needs_sync
    again = control_plan("stop", enabled=False, status="stopped")
    assert (again.enabled, again.needs_sync) == (False, False)


def test_restart_always_syncs_an_enabled_schedule() -> None:
    plan = control_plan("restart", enabled=True, status="running")

    assert (plan.refusal, plan.enabled, plan.needs_sync) == (None, True, True)


def test_restart_of_a_disabled_schedule_is_refused() -> None:
    plan = control_plan("restart", enabled=False, status="stopped")

    assert plan.refusal is not None and "start it instead" in plan.refusal
    assert not plan.needs_sync


def test_an_action_outside_the_three_is_not_read_as_a_start() -> None:
    with pytest.raises(AssertionError):
        control_plan(cast(ControlAction, "pause"), enabled=False, status="stopped")


def test_a_logs_request_always_captures_at_least_a_screenful() -> None:
    assert capture_window(3) == 200
    assert capture_window(200) == 200
    assert capture_window(500) == 500


def test_a_live_capture_shows_its_tail_without_the_blank_screen_rows() -> None:
    source, lines = logs_view(
        2, captured="one\ntwo\nthree\n\n\n  \n", transcript=["old"], last_error="boom"
    )

    assert (source, lines) == ("live", ["two", "three"])


def test_zero_lines_asks_for_everything() -> None:
    assert logs_view(0, captured="a\nb\n\n", transcript=None, last_error=None) == (
        "live",
        ["a", "b"],
    )


def test_a_dead_session_falls_back_to_its_transcript() -> None:
    source, lines = logs_view(5, captured=None, transcript=["a", "b", ""], last_error="boom")

    assert (source, lines) == ("transcript", ["a", "b"])


def test_an_empty_transcript_falls_back_to_the_last_crash() -> None:
    source, lines = logs_view(5, captured=None, transcript=[], last_error="boom\ntrace")

    assert (source, lines) == ("last_error", ["boom", "trace"])


def test_with_nothing_to_show_the_source_is_none() -> None:
    assert logs_view(5, captured=None, transcript=None, last_error=None) == ("none", [])
    assert logs_view(5, captured=None, transcript=None, last_error="") == ("none", [])
