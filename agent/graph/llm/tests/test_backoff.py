from __future__ import annotations

import pytest

from agent.graph.llm._backoff import (
    RETRY_JITTER_SPAN_S,
    WAIT_JITTER_SPAN_S,
    Attempt,
    FailedAttempt,
    RetryPolicy,
    assess_failure,
    backoff_wait,
    budget_spent,
    retry_phase_jitter,
)

# meiva:note
# agent_id % 1000 == 0: the transient schedule starts unshifted.
_POLICY = RetryPolicy(
    agent_id=1000,
    max_attempts=6,
    max_total_seconds=420.0,
    initial_interval_seconds=30.0,
    max_interval_seconds=480.0,
)


def _wait(failed: int = 1, *, remaining: float | None = None, jitter: float = 0.0) -> float | None:
    return backoff_wait(failed, _POLICY, remaining_seconds=remaining, jitter_seconds=jitter)


def test_transient_waits_double_from_the_initial_interval() -> None:
    assert [_wait(failed) for failed in (1, 2, 3, 4)] == [30.0, 60.0, 120.0, 240.0]


def test_a_wait_never_exceeds_the_maximum_interval() -> None:
    assert _wait(5) == 480.0
    capped = RetryPolicy(1000, 6, 420.0, 30.0, 100.0)
    assert [
        backoff_wait(f, capped, remaining_seconds=None, jitter_seconds=0.0) for f in (3, 4, 5)
    ] == [
        100.0,
        100.0,
        100.0,
    ]


def test_the_jitter_roll_rides_on_top_of_the_wait() -> None:
    assert _wait(2, jitter=0.4) == 60.4
    assert _wait(2, jitter=WAIT_JITTER_SPAN_S) == 61.0


def test_the_try_cap_ends_the_node_at_the_capth_failure() -> None:
    assert _wait(5) is not None
    assert _wait(6) is None
    assert _wait(7) is None


def test_a_spent_budget_ends_the_retries() -> None:
    assert _wait(remaining=0.0) is None
    assert _wait(remaining=-3.0) is None


# meiva:note
# The former policy's `max_interval == 0.5` with jitter on, at 1.5s left: the base wait is 0.5s and the jitter (0..1s) rides on top.
def test_the_clipped_wait_is_exactly_the_clipped_base_plus_the_jitter() -> None:
    assert _wait(remaining=1.5, jitter=0.0) == 0.5
    assert _wait(remaining=1.5, jitter=1.0) == 1.5


# meiva:note
# With 1.5s left one second is reserved for the jitter the wait carries, so the base wait is 0.5s and the whole sleep stays inside the budget; at or under a second left there is no jitter and the wait is what remains.
def test_the_last_wait_is_clipped_to_the_remaining_budget() -> None:
    wait = _wait(remaining=1.5, jitter=0.99)
    assert wait is not None and 0.5 <= wait < 1.5
    assert _wait(remaining=0.8, jitter=0.7) == 0.8
    assert _wait(remaining=1.0, jitter=0.7) == 1.0


def test_the_phase_offset_is_stable_per_agent_and_differs_between_agents() -> None:
    assert retry_phase_jitter(11) == retry_phase_jitter(11)
    assert retry_phase_jitter(11) != retry_phase_jitter(22)
    for agent_id in (1, 7, 999, 1000, 123456):
        assert 0.0 <= retry_phase_jitter(agent_id) < RETRY_JITTER_SPAN_S


def test_the_phase_offsets_of_two_named_agents() -> None:
    first, second = retry_phase_jitter(1234), retry_phase_jitter(5678)
    assert first == RETRY_JITTER_SPAN_S * 234 / 1000 and second == RETRY_JITTER_SPAN_S * 678 / 1000
    assert retry_phase_jitter(1000) == 0.0


def test_the_transient_schedule_starts_at_the_agents_phase() -> None:
    for agent_id in (11, 22):
        policy = RetryPolicy(agent_id, 6, 420.0, 30.0, 480.0)
        first = backoff_wait(1, policy, remaining_seconds=None, jitter_seconds=0.0)
        assert first == 30.0 + retry_phase_jitter(agent_id)
    assert retry_phase_jitter(11) != retry_phase_jitter(22)


def test_the_elapsed_time_counts_from_the_first_try_and_never_runs_backwards() -> None:
    attempt = Attempt(number=3, first_started_at=100.0)

    assert attempt.elapsed_seconds(130.0) == 30.0
    assert attempt.elapsed_seconds(90.0) == 0.0


def test_the_budget_is_spent_once_the_total_has_elapsed() -> None:
    attempt = Attempt(number=2, first_started_at=0.0)

    assert not budget_spent(attempt, 419.9, _POLICY)
    assert budget_spent(attempt, 420.0, _POLICY)


@pytest.mark.parametrize(
    ("number", "now", "budget_error", "fatal", "expected"),
    [
        (1, 100.0, False, False, FailedAttempt(320.0, None)),
        (6, 100.0, False, False, FailedAttempt(320.0, "attempts_exhausted")),
        (6, 100.0, False, True, FailedAttempt(320.0, None)),
        (2, 420.0, False, False, FailedAttempt(0.0, "budget_exhausted")),
        (2, 500.0, False, True, FailedAttempt(-80.0, "budget_exhausted")),
        (2, 500.0, True, False, FailedAttempt(-80.0, None)),
        (6, 500.0, True, False, FailedAttempt(-80.0, "attempts_exhausted")),
    ],
)
def test_a_failed_try_names_the_limit_it_hit(
    number: int, now: float, budget_error: bool, fatal: bool, expected: FailedAttempt
) -> None:
    attempt = Attempt(number, first_started_at=0.0)

    assert assess_failure(attempt, now, _POLICY, budget_error=budget_error, fatal=fatal) == expected
