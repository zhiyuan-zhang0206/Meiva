# meiva:note
# What a completed graph invocation asks the host to do next, as pure functions of the result the
# graph returned, the flags a retry carries and the lifecycle command kind the apply committed.
# `finish_completed_invocation` executes the plan these answer; it makes no decision of its own.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


# meiva:note
# Whether a completed graph invocation requested a hosted lifecycle apply (restart or terminate).
def lifecycle_return_requested(result: Mapping[str, object]) -> bool:
    return bool(result["exit_requested"] or result["restart_requested"])


# meiva:note
# After a restart claim `halted` is the claim's verdict on the agent it found: set, it was
# idle; clear, its turn was unfinished and the apply queues the note that continues it.
# meiva:link agent/graph/docs/turn-interrupts.meiva.okf.md
def restart_resumes_turn(result: Mapping[str, object]) -> bool:
    return bool(result["restart_requested"]) and not bool(result["halted"])


# meiva:note
# Apply the lifecycle command the graph returned. `resume_turn` is `restart_resumes_turn`.
@dataclass(frozen=True)
class ApplyLifecycle:
    resume_turn: bool


# meiva:note
# The graph went idle: settle its checkpoint, then report the turn finished.
@dataclass(frozen=True)
class SettleIdle:
    pass


# meiva:note
# Nothing to settle: the invocation did not end the turn and the host invokes the graph again.
@dataclass(frozen=True)
class CarryOn:
    pass


CompletionStep = ApplyLifecycle | SettleIdle | CarryOn


# meiva:note
# The steps `finish_completed_invocation` runs, in order. The two flags record what an earlier
# attempt of the same invocation already made durable, so a retry repeats neither write: the
# checkpoint flush comes first, then the trace correlation (which needs the checkpoint), then one
# closing step chosen from the result.
@dataclass(frozen=True)
class CompletionPlan:
    flush_checkpoint: bool
    attach_trace: bool
    then: CompletionStep


def plan_completion(
    result: Mapping[str, object], *, checkpoint_flushed: bool, trace_attached: bool
) -> CompletionPlan:
    then: CompletionStep
    if lifecycle_return_requested(result):
        then = ApplyLifecycle(resume_turn=restart_resumes_turn(result))
    elif result["turn_idle"]:
        then = SettleIdle()
    else:
        then = CarryOn()
    return CompletionPlan(
        flush_checkpoint=not checkpoint_flushed, attach_trace=not trace_attached, then=then
    )


# meiva:note
# What the host does once the lifecycle command is committed: wake its scheduler for the note that
# continues a resumed turn (only a restart queues one), and report the turn exited when the command
# was a terminate. `kind` is None when the incarnation no longer owned the row; any other kind than
# restart or terminate is refused.
@dataclass(frozen=True)
class LifecycleFollowUp:
    wake_resumed: bool
    exited: bool


# meiva:link services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md
def lifecycle_follow_up(kind: str | None, *, resume_turn: bool) -> LifecycleFollowUp:
    match kind:
        case "restart":
            return LifecycleFollowUp(wake_resumed=resume_turn, exited=False)
        case "terminate":
            return LifecycleFollowUp(wake_resumed=False, exited=True)
        case None:
            return LifecycleFollowUp(wake_resumed=False, exited=False)
        case _:
            raise ValueError(f"unknown lifecycle command kind: {kind!r}")


# meiva:note
# Whether a fresh invocation must ask the checkpoint if a handoff is waiting to resume: only the
# first invocation of a turn (`fresh_input`) of an agent that has an original incarnation can have
# one.
def handoff_resume_wanted(*, fresh_input: bool, has_original_incarnation: bool) -> bool:
    return fresh_input and has_original_incarnation
