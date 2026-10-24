# meiva:note
# The patrol plan decides from values alone: no clock, no loop, no resources.

from __future__ import annotations

import pytest

from services.derived.memory_indexer.patrol_plan import (
    LIVENESS_SAFETY_MARGIN_S,
    LIVENESS_TIMEOUT_FLOOR_S,
    NextPatrol,
    liveness_ceiling_s,
    next_patrol,
    retry_delay_s,
)


def test_retry_delay_doubles_and_never_exceeds_the_patrol_interval() -> None:
    assert [
        retry_delay_s(retry_backoff_s=60.0, patrol_interval_s=900.0, failures=failures)
        for failures in (1, 2, 3, 4)
    ] == [60.0, 120.0, 240.0, 480.0]
    assert [
        retry_delay_s(retry_backoff_s=60.0, patrol_interval_s=200.0, failures=failures)
        for failures in (1, 2, 3)
    ] == [60.0, 120.0, 200.0]


def test_a_complete_pass_waits_the_patrol_interval() -> None:
    assert next_patrol(
        complete=True,
        failures=0,
        over_limit=False,
        retry_backoff_s=60.0,
        patrol_interval_s=900.0,
    ) == NextPatrol("complete", 900.0)


def test_an_incomplete_pass_backs_off_by_its_failure_count() -> None:
    delays = [
        next_patrol(
            complete=False,
            failures=failures,
            over_limit=False,
            retry_backoff_s=60.0,
            patrol_interval_s=900.0,
        )
        for failures in (1, 2, 3)
    ]
    assert delays == [
        NextPatrol("retry", 60.0),
        NextPatrol("retry", 120.0),
        NextPatrol("retry", 240.0),
    ]


def test_an_incomplete_pass_past_the_retry_limit_returns_to_the_patrol_interval() -> None:
    assert next_patrol(
        complete=False,
        failures=9,
        over_limit=True,
        retry_backoff_s=60.0,
        patrol_interval_s=900.0,
    ) == NextPatrol("over_limit", 900.0)


@pytest.mark.parametrize("provider_budget", [0.0, 10.0, 3600.0, 7200.0])
def test_liveness_ceiling_covers_the_worst_embed_batch(provider_budget: float) -> None:
    ceiling = liveness_ceiling_s(provider_budget)
    assert ceiling >= LIVENESS_TIMEOUT_FLOOR_S
    assert ceiling >= provider_budget + LIVENESS_SAFETY_MARGIN_S
    assert ceiling == max(LIVENESS_TIMEOUT_FLOOR_S, provider_budget + LIVENESS_SAFETY_MARGIN_S)
