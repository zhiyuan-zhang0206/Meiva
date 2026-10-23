import pytest

from agent.takeover.gate import (
    DEFAULT_BRIEF,
    DeltaStep,
    GateVerdict,
    LeasePhase,
    SettleStep,
    delta_plan,
    gate_verdict,
    handoff_due,
    settle_step,
    start_brief,
)


@pytest.mark.parametrize(
    ("phase", "verdict"),
    [
        (None, GateVerdict.PROCEED),
        (LeasePhase.REQUESTED, GateVerdict.PREPARE),
        (LeasePhase.ACCEPTED, GateVerdict.PARK),
        (LeasePhase.ACTIVE, GateVerdict.PARK),
        (LeasePhase.ENDED, GateVerdict.PARK),
    ],
)
def test_the_gate_parks_every_lease_except_a_request_the_native_agent_still_accepts(
    phase: LeasePhase | None, verdict: GateVerdict
) -> None:
    assert gate_verdict(phase) is verdict


@pytest.mark.parametrize(
    ("phase", "activate_accepted", "step"),
    [
        (None, True, SettleStep.STOP),
        (LeasePhase.REQUESTED, True, SettleStep.STOP),
        (LeasePhase.ACCEPTED, True, SettleStep.ACTIVATE),
        (LeasePhase.ACCEPTED, False, SettleStep.STOP),
        (LeasePhase.ACTIVE, True, SettleStep.HOLD),
        (LeasePhase.ACTIVE, False, SettleStep.HOLD),
        (LeasePhase.ENDED, True, SettleStep.SETTLE_ENDED),
        (LeasePhase.ENDED, False, SettleStep.SETTLE_ENDED),
    ],
)
def test_settlement_activates_only_when_the_caller_may_activate(
    phase: LeasePhase | None, activate_accepted: bool, step: SettleStep
) -> None:
    assert settle_step(phase, activate_accepted=activate_accepted) is step


def test_every_unapplied_delta_is_applied_when_the_checkpoint_has_no_receipt() -> None:
    plan = delta_plan(lease_id="lease", applied_version=0, delta_version=3, receipt={})
    assert plan == (DeltaStep(1, True), DeltaStep(2, True), DeltaStep(3, True))


def test_a_delta_the_checkpoint_already_carries_only_needs_its_database_receipt() -> None:
    plan = delta_plan(
        lease_id="lease",
        applied_version=0,
        delta_version=3,
        receipt={"lease_id": "lease", "version": 2},
    )
    assert plan == (DeltaStep(1, False), DeltaStep(2, False), DeltaStep(3, True))


def test_a_receipt_for_another_lease_counts_as_zero() -> None:
    plan = delta_plan(
        lease_id="lease",
        applied_version=0,
        delta_version=1,
        receipt={"lease_id": "predecessor", "version": 5},
    )
    assert plan == (DeltaStep(1, True),)


def test_versions_the_database_already_acknowledged_are_not_planned_again() -> None:
    plan = delta_plan(lease_id="lease", applied_version=2, delta_version=3, receipt={})
    assert plan == (DeltaStep(3, True),)
    assert not delta_plan(lease_id="lease", applied_version=3, delta_version=3, receipt={})


@pytest.mark.parametrize(
    ("phase", "receipt_written", "due"),
    [
        (LeasePhase.ENDED, False, True),
        (LeasePhase.ENDED, True, False),
        (LeasePhase.ACTIVE, False, False),
        (LeasePhase.ACCEPTED, False, False),
        (LeasePhase.REQUESTED, False, False),
    ],
)
def test_only_an_ended_lease_without_its_receipt_owes_the_handoff(
    phase: LeasePhase, receipt_written: bool, due: bool
) -> None:
    assert handoff_due(phase, receipt_written=receipt_written) is due


def test_a_request_without_a_stated_purpose_is_accepted_with_the_default_brief() -> None:
    assert start_brief("Fix the login") == "Fix the login"
    assert start_brief("") == DEFAULT_BRIEF
    assert start_brief(None) == DEFAULT_BRIEF
