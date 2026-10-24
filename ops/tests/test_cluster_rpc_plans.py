# meiva:note
# What the cluster RPC client decides, from values: the dispatch budget, the wait between
# attempts, the connect deadline, the meaning of an HTTP status and the business idempotency key.

from __future__ import annotations

import pytest

from ops.cluster.rpc_plans import (
    JITTER_HIGH,
    JITTER_LOW,
    NON_IDEMPOTENT_KINDS,
    DispatchBudget,
    ResponseVerdict,
    RpcConfig,
    business_idempotency_key,
    connect_timeout_s,
    dispatch_budget,
    response_verdict,
    retry_delay_s,
    worst_case_dispatch_seconds,
)

_CONFIG = RpcConfig(timeout_seconds=30.0, max_retries=3)


def test_the_retry_delay_is_a_bounded_exponential_backoff_scaled_by_the_jitter() -> None:
    assert [retry_delay_s(attempt, 1.0) for attempt in (0, 1, 2, 3, 10)] == [
        0.5,
        1.0,
        2.0,
        4.0,
        4.0,
    ]
    assert retry_delay_s(2, JITTER_LOW) == 1.0
    assert retry_delay_s(2, JITTER_HIGH) == 3.0


def test_the_configured_budget_applies_when_the_caller_names_none() -> None:
    assert dispatch_budget(_CONFIG, None, None) == DispatchBudget(timeout_s=30.0, retries=3)


def test_a_callers_budget_overrides_the_configured_one() -> None:
    assert dispatch_budget(_CONFIG, 3.0, 0) == DispatchBudget(timeout_s=3.0, retries=0)


def test_a_negative_retry_count_still_makes_one_attempt() -> None:
    assert dispatch_budget(_CONFIG, None, -2).retries == 0
    assert dispatch_budget(RpcConfig(30.0, -1), None, None).retries == 0


def test_the_worst_case_is_every_attempt_at_the_deadline_plus_the_largest_backoff() -> None:
    config = RpcConfig(timeout_seconds=3.0, max_retries=2)

    # meiva:note
    # Three attempts of 3s, and the backoffs 0.5s and 1.0s at the top of the jitter range.
    assert worst_case_dispatch_seconds(config) == 3 * 3.0 + (0.5 + 1.0) * 1.5
    assert worst_case_dispatch_seconds(config, 5.0) == 3 * 5.0 + (0.5 + 1.0) * 1.5


def test_the_worst_case_without_retries_is_one_attempt() -> None:
    assert worst_case_dispatch_seconds(RpcConfig(timeout_seconds=3.0, max_retries=-4)) == 3.0


def test_the_connect_deadline_never_exceeds_the_attempt_deadline() -> None:
    assert connect_timeout_s(3.0) == 3.0
    assert connect_timeout_s(10.0) == 10.0
    assert connect_timeout_s(30.0) == 10.0


@pytest.mark.parametrize(
    ("status", "verdict"),
    [
        (200, ResponseVerdict.ANSWERED),
        (500, ResponseVerdict.TRANSIENT),
        (503, ResponseVerdict.TRANSIENT),
        (599, ResponseVerdict.TRANSIENT),
        (201, ResponseVerdict.REFUSED),
        (301, ResponseVerdict.REFUSED),
        (400, ResponseVerdict.REFUSED),
        (404, ResponseVerdict.REFUSED),
        (600, ResponseVerdict.REFUSED),
    ],
)
def test_only_a_5xx_is_worth_another_attempt(status: int, verdict: ResponseVerdict) -> None:
    assert response_verdict(status) is verdict


def test_a_launch_key_is_scoped_to_its_target_agent_and_complete_payload() -> None:
    payload = {"agent_id": 42, "launch_attempt_id": "a1"}

    key = business_idempotency_key("runner-a", "spawn-launch", payload)

    assert key is not None and key.startswith("spawn-launch:runner-a:42:")
    assert key == business_idempotency_key(
        "runner-a", "spawn-launch", {"launch_attempt_id": "a1", "agent_id": 42}
    )
    assert key != business_idempotency_key("linux", "spawn-launch", payload)
    assert key != business_idempotency_key(
        "runner-a", "spawn-launch", {"agent_id": 42, "launch_attempt_id": "a2"}
    )


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("lifecycle", {"agent_id": 42}),
        ("status_probe", {}),
        ("spawn-launch", {}),
        ("spawn-launch", {"agent_id": None}),
    ],
)
def test_without_a_business_id_the_transport_mints_the_key(
    kind: str, payload: dict[str, object]
) -> None:
    assert business_idempotency_key("runner-a", kind, payload) is None


def test_the_operations_that_carry_a_key_across_attempts() -> None:
    assert {"spawn-launch", "lifecycle"} == NON_IDEMPOTENT_KINDS
