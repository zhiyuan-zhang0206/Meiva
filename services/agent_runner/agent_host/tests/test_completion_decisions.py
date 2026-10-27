from __future__ import annotations

import pytest

from services.agent_runner.agent_host.invocation.completion_decisions import (
    ApplyLifecycle,
    CarryOn,
    CompletionStep,
    LifecycleFollowUp,
    SettleIdle,
    handoff_resume_wanted,
    lifecycle_follow_up,
    lifecycle_return_requested,
    plan_completion,
    restart_resumes_turn,
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


_QUIET = {"exit_requested": False, "restart_requested": False, "halted": False, "turn_idle": False}


@pytest.mark.parametrize(
    ("result", "then"),
    [
        ({**_QUIET, "exit_requested": True}, ApplyLifecycle(resume_turn=False)),
        ({**_QUIET, "restart_requested": True}, ApplyLifecycle(resume_turn=True)),
        ({**_QUIET, "restart_requested": True, "halted": True}, ApplyLifecycle(resume_turn=False)),
        ({**_QUIET, "turn_idle": True}, SettleIdle()),
        ({**_QUIET}, CarryOn()),
    ],
)
def test_the_closing_step_follows_the_result_the_graph_returned(
    result: dict[str, bool], then: CompletionStep
) -> None:
    plan = plan_completion(result, checkpoint_flushed=False, trace_attached=False)

    assert plan.then == then


# meiva:note
# A lifecycle return outranks an idle turn: the apply settles the checkpoint itself.
def test_a_lifecycle_return_outranks_an_idle_turn() -> None:
    result = {**_QUIET, "exit_requested": True, "turn_idle": True}

    plan = plan_completion(result, checkpoint_flushed=True, trace_attached=True)

    assert plan.then == ApplyLifecycle(resume_turn=False)


@pytest.mark.parametrize(
    ("checkpoint_flushed", "trace_attached", "flush", "attach"),
    [(False, False, True, True), (True, False, False, True), (True, True, False, False)],
)
def test_a_retry_repeats_neither_write_an_earlier_attempt_made_durable(
    checkpoint_flushed: bool, trace_attached: bool, flush: bool, attach: bool
) -> None:
    plan = plan_completion(
        _QUIET, checkpoint_flushed=checkpoint_flushed, trace_attached=trace_attached
    )

    assert (plan.flush_checkpoint, plan.attach_trace) == (flush, attach)


@pytest.mark.parametrize(
    ("kind", "resume_turn", "wake_resumed", "exited"),
    [
        ("restart", True, True, False),
        ("restart", False, False, False),
        ("terminate", True, False, True),
        ("terminate", False, False, True),
        (None, True, False, False),
    ],
)
def test_the_committed_command_decides_the_wake_and_the_exit(
    kind: str | None, resume_turn: bool, wake_resumed: bool, exited: bool
) -> None:
    assert lifecycle_follow_up(kind, resume_turn=resume_turn) == LifecycleFollowUp(
        wake_resumed=wake_resumed, exited=exited
    )


@pytest.mark.parametrize(
    ("fresh_input", "has_original_incarnation", "expected"),
    [(True, True, True), (True, False, False), (False, True, False), (False, False, False)],
)
def test_only_a_turns_first_invocation_of_an_incarnated_agent_checks_for_a_handoff(
    fresh_input: bool, has_original_incarnation: bool, expected: bool
) -> None:
    assert (
        handoff_resume_wanted(
            fresh_input=fresh_input, has_original_incarnation=has_original_incarnation
        )
        is expected
    )


def test_a_lifecycle_command_kind_the_host_does_not_know_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown lifecycle command kind: 'pause'"):
        lifecycle_follow_up("pause", resume_turn=False)
