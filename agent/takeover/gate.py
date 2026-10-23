from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

DEFAULT_BRIEF = "Continue the agent's work from its saved state."


class LeasePhase(Enum):
    REQUESTED = "requested"
    ACCEPTED = "accepted"
    ACTIVE = "active"
    ENDED = "ended"


class GateVerdict(Enum):
    PROCEED = "proceed"
    PREPARE = "prepare"
    PARK = "park"


class SettleStep(Enum):
    STOP = "stop"
    ACTIVATE = "activate"
    HOLD = "hold"
    SETTLE_ENDED = "settle_ended"


@dataclass(frozen=True)
class DeltaStep:
    version: int
    apply: bool


# meiva:note
# What the native claim gate and the hook guards do with the lease the database
# reports. No lease leaves the native graph running. A request waits for the native
# agent to accept it (without a model decision). An accepted, active or ended lease
# whose settlement is still owed parks the graph at the gate.
# meiva:link agent/docs/impersonation/impersonation.meiva.okf.md
def gate_verdict(phase: LeasePhase | None) -> GateVerdict:
    if phase is None:
        return GateVerdict.PROCEED
    if phase is LeasePhase.REQUESTED:
        return GateVerdict.PREPARE
    return GateVerdict.PARK


# meiva:note
# The next move of the settlement that follows an invocation and its checkpoint
# flush. A request is not settled yet. An accepted lease activates only when this
# call may activate (a lifecycle control pass must not). An active lease keeps the
# native graph parked. An ended lease applies its plugin deltas and owes the
# end-of-session note.
# meiva:link agent/docs/impersonation/impersonation.meiva.okf.md
def settle_step(phase: LeasePhase | None, *, activate_accepted: bool) -> SettleStep:
    if phase is None or phase is LeasePhase.REQUESTED:
        return SettleStep.STOP
    if phase is LeasePhase.ACCEPTED:
        return SettleStep.ACTIVATE if activate_accepted else SettleStep.STOP
    if phase is LeasePhase.ACTIVE:
        return SettleStep.HOLD
    return SettleStep.SETTLE_ENDED


# meiva:note
# Which plugin deltas of an ended lease still change the checkpoint. The log version
# the checkpoint already carries for this lease (its receipt) is applied, so a delta
# at or below it only needs its database receipt: replaying an additive reducer
# would count the delta twice. A receipt for another lease counts as zero.
# meiva:link agent/tests/impersonation/docs/checkpoint-receipt.meiva.okf.md
def delta_plan(
    *,
    lease_id: str,
    applied_version: int,
    delta_version: int,
    receipt: Mapping[str, str | int],
) -> tuple[DeltaStep, ...]:
    version = receipt.get("version", 0)
    recorded = version if receipt.get("lease_id") == lease_id and isinstance(version, int) else 0
    return tuple(
        DeltaStep(version=number, apply=recorded < number)
        for number in range(applied_version + 1, delta_version + 1)
    )


# meiva:note
# Whether an ended lease still owes the native checkpoint its receipt: the
# end-of-session note plus `handoff_applied_at`. Until it lands, the agent refuses every
# new takeover as an unapplied state.
# meiva:link docs/conventions/agents/agent-impersonation.md
def handoff_due(phase: LeasePhase, *, receipt_written: bool) -> bool:
    return phase is LeasePhase.ENDED and not receipt_written


def start_brief(reason: str | None) -> str:
    return reason or DEFAULT_BRIEF
