# meiva:note
# The op a lifecycle request becomes and the error a failed runner stands for: values in, plan out.

from __future__ import annotations

import pytest

from base.api_contracts.agent_contract import (
    EXCEPTION_BY_REASON,
    CrossMachineGatewayUnavailable,
    ErrorReason,
    MachineNotRegistered,
)
from gateway.agents.launch.lifecycle_plan import (
    lifecycle_op,
    lifecycle_timeout,
    lifecycle_unreachable,
    proxied_failure,
)


def test_a_lifecycle_request_becomes_one_lifecycle_op() -> None:
    op = lifecycle_op(
        "/api/agents/7/terminate",
        {"force": True},
        timeout_s=5.0,
        retries=1,
        idempotency_key="intent",
    )
    assert (op.kind, op.payload, op.timeout_s, op.retries, op.idempotency_key) == (
        "lifecycle",
        {"path": "/api/agents/7/terminate", "body": {"force": True}},
        5.0,
        1,
        "intent",
    )


def test_the_op_owns_a_copy_of_the_request_body() -> None:
    body: dict[str, object] = {"force": True}
    op = lifecycle_op("/p", body, timeout_s=1.0, retries=0, idempotency_key=None)
    body["force"] = False
    assert op.payload["body"] == {"force": True}
    assert op.idempotency_key is None


def test_a_silent_runner_is_named_with_its_deadline() -> None:
    error = lifecycle_timeout("runner-a", 12.0)
    assert isinstance(error, CrossMachineGatewayUnavailable)
    assert str(error) == "target machine='runner-a' lifecycle op did not answer within 12s"


def test_an_unreachable_runner_carries_the_transport_cause() -> None:
    error = lifecycle_unreachable("runner-a", "connect refused")
    assert isinstance(error, CrossMachineGatewayUnavailable)
    assert "runner-a" in str(error)
    assert str(error).endswith("ops server unreachable for lifecycle op: connect refused")


@pytest.mark.parametrize("reason", list(EXCEPTION_BY_REASON))
def test_every_wire_reason_comes_back_as_its_own_error(reason: ErrorReason) -> None:
    failure = proxied_failure({"error": "X: y", "detail": "the detail", "reason": reason.value})
    assert type(failure.error) is EXCEPTION_BY_REASON[reason]
    assert str(failure.error) == "the detail"
    assert failure.unknown_reason is None


def test_a_reason_the_gateway_has_no_error_for_is_reported_and_generic() -> None:
    payload = {"error": "X: y", "detail": "d", "reason": "reason_from_a_newer_runner"}
    failure = proxied_failure(payload)
    assert isinstance(failure.error, CrossMachineGatewayUnavailable)
    assert failure.unknown_reason == "reason_from_a_newer_runner"
    assert "reason_from_a_newer_runner" in str(failure.error)


@pytest.mark.parametrize(
    "payload",
    [
        {"error": "boom"},
        {"error": "boom", "reason": "machine_not_registered"},
        {"error": "boom", "detail": "no reason"},
        {"unexpected": True},
        {},
    ],
)
def test_a_failure_without_a_usable_reason_is_generic_and_carries_the_payload(
    payload: dict[str, object],
) -> None:
    failure = proxied_failure(payload)
    assert isinstance(failure.error, CrossMachineGatewayUnavailable)
    assert not isinstance(failure.error, MachineNotRegistered)
    assert failure.unknown_reason is None
    assert repr(payload) in str(failure.error)
