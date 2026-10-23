import asyncio

import pytest

from agent.graph.interrupt._plan import (
    ExitVerdict,
    FailureAction,
    Fire,
    WatchStep,
    exit_verdict,
    failure_action,
    watch_step,
    watcher_fate,
)
from base.agents.messages.inbound import InterruptReason


@pytest.mark.parametrize(
    ("reason", "stopped", "step"),
    [
        (None, False, WatchStep.WAIT),
        (InterruptReason.USER, False, Fire(InterruptReason.USER)),
        (InterruptReason.RESTART, False, Fire(InterruptReason.RESTART)),
        (None, True, WatchStep.EXIT),
        (InterruptReason.USER, True, WatchStep.EXIT),
    ],
)
def test_a_poll_fires_only_while_the_invocation_still_wants_the_event(
    reason: InterruptReason | None, stopped: bool, step: Fire | WatchStep
) -> None:
    assert watch_step(reason, stopped=stopped) == step


@pytest.mark.parametrize(
    ("late", "stopped", "action"),
    [
        (False, False, FailureAction.WARN_AND_INTERRUPT),
        (False, True, FailureAction.WARN),
        (True, False, FailureAction.REPORT_TO_SERVICE),
        (True, True, FailureAction.REPORT_TO_SERVICE),
    ],
)
def test_a_failed_watcher_aborts_its_node_unless_it_was_already_handed_to_the_service(
    late: bool, stopped: bool, action: FailureAction
) -> None:
    assert failure_action(late=late, stopped=stopped) is action


def test_the_fate_of_a_retained_watcher_reads_as_its_error_its_cancellation_or_a_plain_return() -> (
    None
):
    error = ValueError("poll failed")
    assert watcher_fate(error, cancelled=False) == f"raised {error!r}"
    assert watcher_fate(None, cancelled=True) == "cancelled"
    assert watcher_fate(None, cancelled=False) == "returned normally"


_WATCHER = ValueError("watcher cleanup failed")
_PRIMARY = RuntimeError("body failed")
_CALLER_CANCEL = asyncio.CancelledError()


def test_a_clean_unwind_raises_nothing_unless_the_caller_was_cancelled_with_no_primary_failure() -> (
    None
):
    quiet = ExitVerdict(retain=False, raised=None, cause=None, note=None)
    assert exit_verdict(unwound=True, error=None, primary=None, cancellation=None) == quiet
    assert exit_verdict(unwound=True, error=None, primary=_PRIMARY, cancellation=None) == quiet
    assert (
        exit_verdict(unwound=True, error=None, primary=_PRIMARY, cancellation=_CALLER_CANCEL)
        == quiet
    )
    cancelled = exit_verdict(unwound=True, error=None, primary=None, cancellation=_CALLER_CANCEL)
    assert cancelled == ExitVerdict(retain=False, raised=_CALLER_CANCEL, cause=None, note=None)


def test_a_watcher_still_running_at_the_deadline_is_retained_and_still_honours_the_cancel() -> None:
    retained = exit_verdict(unwound=False, error=None, primary=None, cancellation=None)
    assert retained == ExitVerdict(retain=True, raised=None, cause=None, note=None)
    with_cancel = exit_verdict(unwound=False, error=None, primary=None, cancellation=_CALLER_CANCEL)
    assert with_cancel.retain
    assert with_cancel.raised is _CALLER_CANCEL
    with_primary = exit_verdict(
        unwound=False, error=None, primary=_PRIMARY, cancellation=_CALLER_CANCEL
    )
    assert with_primary == ExitVerdict(retain=True, raised=None, cause=None, note=None)


def test_a_watcher_error_with_nothing_else_in_flight_reaches_the_caller_unwrapped() -> None:
    verdict = exit_verdict(unwound=True, error=_WATCHER, primary=None, cancellation=None)
    assert verdict.raised is _WATCHER
    assert verdict.cause is None
    assert verdict.note is None
    assert not verdict.retain


def test_a_watcher_error_beside_a_primary_failure_keeps_the_primary_and_groups_the_secondary() -> (
    None
):
    verdict = exit_verdict(unwound=True, error=_WATCHER, primary=_PRIMARY, cancellation=None)
    assert verdict.raised is _PRIMARY
    assert verdict.cause is not None
    assert verdict.cause.exceptions == (_WATCHER,)
    assert verdict.note == f"interrupt watcher cleanup also failed: {_WATCHER!r}"


def test_the_primary_failures_own_cause_stays_first_in_the_group() -> None:
    primary = RuntimeError("body failed")
    original = KeyError("original")
    primary.__cause__ = original
    verdict = exit_verdict(unwound=True, error=_WATCHER, primary=primary, cancellation=None)
    assert verdict.cause is not None
    assert verdict.cause.exceptions == (original, _WATCHER)


def test_a_caller_cancellation_stands_in_for_the_primary_failure_when_there_is_none() -> None:
    verdict = exit_verdict(unwound=True, error=_WATCHER, primary=None, cancellation=_CALLER_CANCEL)
    assert verdict.raised is _CALLER_CANCEL
    assert verdict.cause is not None
    assert verdict.cause.exceptions == (_WATCHER,)
    both = exit_verdict(unwound=True, error=_WATCHER, primary=_PRIMARY, cancellation=_CALLER_CANCEL)
    assert both.raised is _PRIMARY
