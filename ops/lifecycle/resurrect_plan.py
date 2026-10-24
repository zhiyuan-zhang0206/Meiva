# meiva:note
# Whether and where an automatic wake of a terminated agent runs, decided from values.
#
# The shell (`ops.lifecycle.resurrect_if_terminated`) reads a terminated agent's gate facts and
# executes the plan made here: skip because the recovery breaker is tripped or the trigger is a
# notification, or forward the wake to the agent's home machine. A remote-homed agent is never
# launched on the machine that happened to receive the input.

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from base.agents.messages.inbound import InboundKind, WakeTriggerKind, validate_wake_trigger_kind
from base.api_contracts.agent_contract import ResurrectResult

__all__ = [
    "AfterSpawn",
    "ClearWakeSuppression",
    "Forward",
    "NotificationOnly",
    "RecoveryHalted",
    "RereadStatus",
    "ResurrectFacts",
    "ResurrectPlan",
    "plan_after_result",
    "plan_resurrection",
]


# meiva:note
# What the shell read about one terminated agent and the input that wants it awake.
#
# `recovery_halted` is the breaker's verdict (consecutive permanent provider rejections);
# `notice_source` is the trigger's source when it is a system-family chat notice, else None;
# `home` is the machine the agent is placed on.
@dataclass(frozen=True)
class ResurrectFacts:
    agent_id: int
    trigger_inbound_id: int
    trigger_kind: WakeTriggerKind
    recovery_halted: bool
    notice_source: str | None
    home: str
    local_machine: str


# meiva:note
# A background trigger after a permanent provider rejection: only a new chat may try again.
@dataclass(frozen=True)
class RecoveryHalted: ...


# meiva:note
# The trigger is a system notice from `source`; notifications never resurrect their owner.
@dataclass(frozen=True)
class NotificationOnly:
    source: str


# meiva:note
# Ask the home machine to run the wake for one exact trigger input at `path`.
#
# `run_here_if_unreachable` is true only when the home machine is this one: its ops server being
# unreachable then means a single-process deployment, and the wake runs in this process. An
# unreachable remote home is never launched from here.
@dataclass(frozen=True)
class Forward:
    home: str
    path: str
    trigger_inbound_id: int
    trigger_inbound_kind: WakeTriggerKind
    run_here_if_unreachable: bool


ResurrectPlan = RecoveryHalted | NotificationOnly | Forward


# meiva:note
# Clear the wake suppression a successful resurrection makes stale.
@dataclass(frozen=True)
class ClearWakeSuppression: ...


# meiva:note
# Read the status again, because the wake changed it.
@dataclass(frozen=True)
class RereadStatus: ...


AfterSpawn = ClearWakeSuppression | RereadStatus


# meiva:note
# The plan for one terminated agent's automatic wake.
#
# A permanent provider rejection stops every background trigger but not a new chat, which is an
# explicit attempt after the fix. The breaker is judged before the notification rule, and a trigger
# kind that cannot wake an agent is a `ValueError`.
def plan_resurrection(facts: ResurrectFacts) -> ResurrectPlan:
    kind = validate_wake_trigger_kind(facts.trigger_kind)
    if kind is not InboundKind.CHAT and facts.recovery_halted:
        return RecoveryHalted()
    if facts.notice_source is not None:
        return NotificationOnly(facts.notice_source)
    return Forward(
        home=facts.home,
        path=f"/api/agents/{facts.agent_id}/resurrect-if-pending-work",
        trigger_inbound_id=facts.trigger_inbound_id,
        trigger_inbound_kind=kind,
        run_here_if_unreachable=facts.home == facts.local_machine,
    )


# meiva:note
# What follows a wake answer: a spawn clears the suppression and refreshes the status.
def plan_after_result(result: ResurrectResult) -> tuple[AfterSpawn, ...]:
    match result:
        case ResurrectResult.SPAWNED:
            return (ClearWakeSuppression(), RereadStatus())
        case ResurrectResult.ALREADY_ALIVE:
            return ()
        case _:
            assert_never(result)
