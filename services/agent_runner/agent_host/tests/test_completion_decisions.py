from __future__ import annotations

import pytest

from services.agent_runner.agent_host.invocation.completion_decisions import (
    lifecycle_return_requested,
    restart_resumes_turn,
    should_wake_resumed,
)


@pytest.mark.parametrize(
    ("exit_requested", "restart_requested", "expected"),
    [(False, False, False), (True, False, True), (False, True, True), (True, True, True)],
)
def test_a_lifecycle_apply_is_requested_by_either_exit_or_restart(
    exit_requested: bool, restart_requested: bool, expected: bool
) -> None:
    result = {"exit_requested": exit_requested, "restart_requested": restart_requested}
    assert lifecycle_return_requested(result) is expected


@pytest.mark.parametrize(
    ("restart_requested", "halted", "expected"),
    [
        (True, False, True),
        (True, True, False),
        (False, False, False),
        (False, True, False),
    ],
)
def test_only_a_restart_that_found_an_unfinished_turn_resumes_it(
    restart_requested: bool, halted: bool, expected: bool
) -> None:
    result = {"restart_requested": restart_requested, "halted": halted}
    assert restart_resumes_turn(result) is expected


@pytest.mark.parametrize(
    ("kind", "resume_turn", "expected"),
    [
        ("restart", True, True),
        ("restart", False, False),
        ("terminate", True, False),
        (None, True, False),
    ],
)
def test_the_scheduler_wakes_only_for_a_committed_restart_that_resumes_a_turn(
    kind: str | None, resume_turn: bool, expected: bool
) -> None:
    assert should_wake_resumed(kind, resume_turn=resume_turn) is expected
