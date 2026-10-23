# meiva:note
# The decisions of a spawn launch, as values in and a returned error or payload out.

from __future__ import annotations

from uuid import uuid4

import pytest

from base.agents.observation.evidence import AvailabilityReason
from base.api_contracts.agent_contract import MachinePaused, SpawnTargetNotAgentRunner
from gateway.agents.launch.launch_plan import (
    LaunchForwardError,
    launch_failure,
    launch_op_payload,
    launch_receipt,
    launch_rejection,
    launch_unreachable,
    spawn_target_refusal,
)
from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent


def test_a_target_without_the_runner_capability_is_refused_before_the_pause_latch() -> None:
    refusal = spawn_target_refusal("gw-only", ["gateway"], paused=True)
    assert isinstance(refusal, SpawnTargetNotAgentRunner)
    assert "'gw-only'" in str(refusal)
    assert "agent-runner" in str(refusal)


def test_a_paused_runner_is_refused_with_the_resume_instruction() -> None:
    refusal = spawn_target_refusal("away", ["gateway", "agent-runner"], paused=True)
    assert isinstance(refusal, MachinePaused)
    assert "meiva cluster resume" in str(refusal)


@pytest.mark.parametrize("roles", [["agent-runner"], ["gateway", "agent-runner"]])
def test_a_live_runner_is_not_refused(roles: list[str]) -> None:
    assert spawn_target_refusal("runner", roles, paused=False) is None


def test_the_launch_payload_carries_the_attempt_and_omits_what_is_unset() -> None:
    attempt = uuid4()
    payload = launch_op_payload(LaunchAgentRequest(agent_id=4, launch_attempt_id=attempt))
    assert payload == {"agent_id": 4, "launch_attempt_id": str(attempt)}


def test_an_unreachable_runner_names_the_target_and_the_cause() -> None:
    error = launch_unreachable("runner-a", "connect refused")
    assert error.reason == AvailabilityReason.LAUNCH_UNREACHABLE
    assert "'runner-a'" in str(error)
    assert "connect refused" in str(error)


def test_a_rejection_keeps_the_runner_s_business_reason() -> None:
    error = launch_rejection(
        "runner-a",
        {
            "error": "InvalidModelConfig: key missing",
            "reason": "invalid_model_config",
            "detail": "key missing",
        },
    )
    assert error.reason == AvailabilityReason.LAUNCH_REJECTED
    assert "invalid_model_config: key missing" in str(error)


def test_a_rejection_without_a_reason_falls_back_to_the_error_text() -> None:
    error = launch_rejection("runner-a", {"error": "boom"})
    assert error.reason == AvailabilityReason.LAUNCH_REJECTED
    assert "boom: boom" in str(error)


def test_a_rejection_that_is_not_an_op_failure_reads_as_unreachable() -> None:
    error = launch_rejection("runner-a", {"unexpected": True})
    assert error.reason == AvailabilityReason.LAUNCH_UNREACHABLE
    assert "malformed launch failure" in str(error)


def test_a_receipt_for_the_asked_agent_is_accepted() -> None:
    receipt = launch_receipt("runner-a", {"id": 4}, agent_id=4)
    assert receipt == SpawnedAgent(id=4)


@pytest.mark.parametrize(
    ("result", "needle"),
    [
        ({"id": "not-a-number"}, "malformed launch receipt"),
        ({}, "malformed launch receipt"),
        ({"id": 5}, "returned agent 5 for 4"),
    ],
)
def test_a_receipt_that_is_malformed_or_for_another_agent_is_an_error(
    result: dict[str, object], needle: str
) -> None:
    receipt = launch_receipt("runner-a", result, agent_id=4)
    assert isinstance(receipt, LaunchForwardError)
    assert receipt.reason == AvailabilityReason.LAUNCH_UNREACHABLE
    assert needle in str(receipt)


def test_a_typed_launch_error_is_recorded_with_its_own_reason_and_text() -> None:
    error = LaunchForwardError(AvailabilityReason.LAUNCH_REJECTED, "runner said no")
    failure = launch_failure(error)
    assert (failure.reason, failure.detail) == (
        AvailabilityReason.LAUNCH_REJECTED,
        "runner said no",
    )


def test_any_other_error_is_recorded_as_unknown_with_only_its_type() -> None:
    failure = launch_failure(TimeoutError("acknowledgement lost, secret detail"))
    assert (failure.reason, failure.detail) == (AvailabilityReason.LAUNCH_UNKNOWN, "TimeoutError")
