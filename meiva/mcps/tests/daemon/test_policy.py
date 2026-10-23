from __future__ import annotations

import pytest

from meiva.mcps._policy import RetryPolicy, retry_delay

DEFAULT = RetryPolicy()


def test_the_default_policy_is_three_tries_with_a_doubling_delay_capped_at_eight() -> None:
    assert (DEFAULT.max_attempts, DEFAULT.base_delay_s, DEFAULT.max_delay_s) == (3, 1.0, 8.0)


# meiva:note
# 3 tries = 2 retries: failed try 0 waits 1 s, failed try 1 waits 2 s, failed try 2 ends.
def test_each_retry_waits_twice_as_long_and_the_last_try_ends_the_request() -> None:
    assert [retry_delay(DEFAULT, attempt, transport_error=True) for attempt in (0, 1, 2)] == [
        1.0,
        2.0,
        None,
    ]


def test_a_failure_that_is_not_a_transport_error_never_retries() -> None:
    assert [retry_delay(DEFAULT, attempt, transport_error=False) for attempt in (0, 1, 2)] == [
        None,
        None,
        None,
    ]


def test_the_delay_is_capped() -> None:
    policy = RetryPolicy(max_attempts=10, base_delay_s=1.0, max_delay_s=8.0)
    assert [retry_delay(policy, attempt, transport_error=True) for attempt in range(6)] == [
        1.0,
        2.0,
        4.0,
        8.0,
        8.0,
        8.0,
    ]


@pytest.mark.parametrize("attempts", [1, 2, 5])
def test_the_last_allowed_try_never_waits(attempts: int) -> None:
    policy = RetryPolicy(max_attempts=attempts, base_delay_s=0.5, max_delay_s=8.0)
    assert retry_delay(policy, attempts - 1, transport_error=True) is None
    if attempts > 1:
        assert retry_delay(policy, attempts - 2, transport_error=True) is not None


def test_a_test_can_ask_for_retries_without_waiting() -> None:
    policy = RetryPolicy(max_attempts=3, base_delay_s=0.0, max_delay_s=0.0)
    assert retry_delay(policy, 0, transport_error=True) == 0.0
