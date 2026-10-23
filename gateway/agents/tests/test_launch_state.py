# meiva:note
# What a failed launch dispatch is reported with, from the facts the shell read back.

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from base.agents.observation.evidence import AgentAvailability, AvailabilityReason
from gateway.agents.launch.launch_state import (
    STORE_UNREADABLE,
    accepted_launch,
    launch_failed_error,
    launch_state,
)
from ops.rpc_schemas import SpawnedAgent

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
UNREACHABLE = AgentAvailability(reason=AvailabilityReason.LAUNCH_UNREACHABLE, observed_at=NOW)


def test_an_idle_unadmitted_agent_with_an_attempt_may_be_retried() -> None:
    state = launch_state("idling", None, uuid4(), UNREACHABLE)
    assert (state.admitted, state.retry_legal) == (False, True)
    assert state.view == {"status": "idling", "availability": UNREACHABLE.model_dump(mode="json")}


def test_an_admitted_agent_won_the_race_and_is_not_retried() -> None:
    state = launch_state("running", NOW, uuid4(), UNREACHABLE)
    assert (state.admitted, state.retry_legal) == (True, False)


def test_a_terminated_agent_is_not_admitted_even_with_an_admission_stamp() -> None:
    state = launch_state("terminated", NOW, uuid4(), None)
    assert (state.admitted, state.retry_legal) == (False, False)
    assert state.view == {"status": "terminated", "availability": None}


def test_an_agent_without_an_attempt_cannot_be_retried() -> None:
    assert launch_state("idling", None, None, None).retry_legal is False


def test_a_row_that_is_gone_is_unknown_and_not_retryable() -> None:
    state = launch_state(None, None, None, None)
    assert state.view == {"status": "unknown", "availability": None}
    assert (state.admitted, state.retry_legal) == (False, False)


def test_an_unreadable_store_is_unknown_but_leaves_the_retry_legal() -> None:
    assert STORE_UNREADABLE.view == {"status": "unknown", "availability": None}
    assert (STORE_UNREADABLE.admitted, STORE_UNREADABLE.retry_legal) == (False, True)


def test_the_failure_names_the_agent_the_target_and_the_retry_path() -> None:
    error = launch_failed_error(
        7, "runner-a", "runner offline", launch_state("idling", None, 1, None)
    )
    assert error.agent_id == 7
    assert "Agent #7 was created" in str(error)
    assert "runner-a" in str(error)
    assert error.retry_launch_path == "/api/keyed/v1/agents/7/retry-launch"


def test_a_failure_that_cannot_be_retried_advertises_no_path() -> None:
    error = launch_failed_error(
        7, "runner-a", "runner offline", launch_state("terminated", None, 1, None)
    )
    assert error.retry_launch_path is None


def test_an_accepted_launch_reports_what_was_observed_and_no_execution() -> None:
    receipt = accepted_launch(SpawnedAgent(id=3), UNREACHABLE, None)
    assert (receipt.accepted, receipt.execution_observed) == (True, False)
    assert (receipt.reason, receipt.observed_at) == (AvailabilityReason.LAUNCH_UNREACHABLE, NOW)
    assert receipt.config_normalized is None


def test_an_accepted_launch_carries_the_model_rewrite_it_made() -> None:
    receipt = accepted_launch(SpawnedAgent(id=3), UNREACHABLE, ("retired-model", "fallback-model"))
    assert receipt.config_normalized is not None
    assert (receipt.config_normalized.requested, receipt.config_normalized.resolved) == (
        "retired-model",
        "fallback-model",
    )
