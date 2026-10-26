from __future__ import annotations

import random
from collections.abc import Callable

import pytest
from redis.exceptions import AuthenticationError

from base.events.live.bus import EventBus, event_bus_config_from_settings
from base.events.live.redis_client import retry_auth_failures_async, retry_auth_failures_sync
from base.events.live.redis_resilience import (
    AuthRetry,
    AuthRetryPolicy,
    compute_auth_retry_delay,
)

_POLICY = AuthRetryPolicy(initial_delay_s=0.0005, delay_cap_s=0.002, window_s=1.0, max_attempts=10)


# meiva:note
# A generator whose draws the test can reproduce with another one built from the same seed.
def _rng(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311


# meiva:note
# What a failed attempt waits before the next one, with the jitter sample (in [0, 1]) the retry
# drew: half the exponential cap at 0 and the full cap at 1, never past what is left of the window.
@pytest.mark.parametrize(
    ("failure_count", "sample", "expected"),
    [
        (1, 0.0, 0.25),
        (1, 1.0, 0.5),
        (2, 0.5, 0.75),
        (3, 1.0, 2.0),
        (6, 1.0, 10.0),
        (6, 0.0, 5.0),
    ],
)
def test_a_delay_sits_between_half_the_exponential_cap_and_the_cap(
    failure_count: int, sample: float, expected: float
) -> None:
    assert compute_auth_retry_delay(
        failure_count=failure_count, waited_s=0.0, elapsed_s=0.0, jitter_sample=sample
    ) == pytest.approx(expected)


def test_the_delays_of_a_run_of_failures_stay_inside_the_window() -> None:
    delays: list[float] = []
    waited = 0.0
    for failure_count in range(1, 100):
        delay = compute_auth_retry_delay(
            failure_count=failure_count, waited_s=waited, elapsed_s=waited, jitter_sample=1.0
        )
        if delay <= 0:
            break
        delays.append(delay)
        waited += delay
    assert delays[:4] == [0.5, 1.0, 2.0, 4.0]
    assert sum(delays) <= 60.0
    assert max(delays) <= 10.0


def _delays(retry: AuthRetry, failures: int) -> list[float]:
    return [
        retry.next_delay(failure_count=count, waited_s=0.0, elapsed_s=0.0)
        for count in range(1, failures + 1)
    ]


def test_the_retry_places_each_delay_by_a_sample_from_the_generator_it_was_given() -> None:
    reference = _rng(5)
    expected = [
        compute_auth_retry_delay(
            failure_count=count,
            waited_s=0.0,
            elapsed_s=0.0,
            jitter_sample=reference.random(),
            policy=_POLICY,
        )
        for count in range(1, 5)
    ]
    assert _delays(AuthRetry(_POLICY, rng=_rng(5)), 4) == expected


def test_generators_with_different_seeds_give_different_delays() -> None:
    first = _delays(AuthRetry(_POLICY, rng=_rng(1)), 6)
    second = _delays(AuthRetry(_POLICY, rng=_rng(2)), 6)
    assert first != second
    assert len(set(first)) == len(first)


def test_the_bus_owns_one_retry_over_its_generator() -> None:
    bus = EventBus(event_bus_config_from_settings(), rng=_rng(7))
    policy = bus.auth_retry.policy
    reference = AuthRetry(policy, rng=_rng(7))
    assert _delays(bus.auth_retry, 3) == _delays(reference, 3)


def _failing(times: int) -> Callable[[], str]:
    state = {"left": times}

    def operation() -> str:
        if state["left"] > 0:
            state["left"] -= 1
            raise AuthenticationError("ACL is being re-affirmed")
        return "done"

    return operation


# meiva:note
# The retry loop is what makes the delays differ between processes, so the regression is that it
# draws one sample per retry from the generator it was handed: a loop that ignores it (a constant
# sample, a fixed delay) leaves the generator where it was.
def test_the_sync_loop_draws_one_jitter_sample_per_retry() -> None:
    rng = _rng(11)
    operation = _failing(3)

    result = retry_auth_failures_sync(operation, auth_retry=AuthRetry(_POLICY, rng=rng))

    assert result == "done"
    reference = _rng(11)
    for _ in range(3):
        reference.random()
    assert rng.getstate() == reference.getstate()


async def test_the_async_loop_draws_one_jitter_sample_per_retry() -> None:
    rng = _rng(13)
    state = {"left": 2}

    async def operation() -> str:
        if state["left"] > 0:
            state["left"] -= 1
            raise AuthenticationError("ACL is being re-affirmed")
        return "done"

    result = await retry_auth_failures_async(operation, auth_retry=AuthRetry(_POLICY, rng=rng))

    assert result == "done"
    reference = _rng(13)
    for _ in range(2):
        reference.random()
    assert rng.getstate() == reference.getstate()


def test_the_loop_gives_up_when_the_attempts_are_spent() -> None:
    policy = AuthRetryPolicy(initial_delay_s=0.0001, delay_cap_s=0.0002, max_attempts=3)
    attempts = {"n": 0}

    def operation() -> str:
        attempts["n"] += 1
        raise AuthenticationError("still not affirmed")

    with pytest.raises(AuthenticationError):
        retry_auth_failures_sync(operation, auth_retry=AuthRetry(policy, rng=_rng(1)))

    assert attempts["n"] == 3
