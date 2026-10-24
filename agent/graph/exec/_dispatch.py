# meiva:note
# The decision half of exec result dispatch: which outcome an `ExecResult` is, whether the turn
# halts, the status and the text handed to the envelope, and which markers the envelope carries.
# `node.dispatch_exec_result` wraps, logs and announces what this plan says.
# meiva:link agent/graph/exec/docs/exec.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from base.agents.messages.inbound import InterruptReason
from base.agents.messages.kwargs import ExecStatus

from ._result import (
    ExecCancelled,
    ExecCrashed,
    ExecDone,
    ExecLifecycle,
    ExecLifecycleKind,
    ExecResult,
    ExecTimedOut,
)

SYSTEM_HALT_NOTE = (
    "[system halt] You just called meiva.self.compact; your context has been compacted "
    "and you will continue as the same agent\n"
)


class DispatchKind(Enum):
    DONE = "done"
    COMPACT = "compact"
    LIFECYCLE = "lifecycle"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    CRASHED = "crashed"


# meiva:note
# What the shell logs about a crash: the exception, and the type and message the child reported
# (or the parent-side exception's own) that the operator-facing signals carry.
@dataclass(frozen=True)
class CrashFacts:
    exc: BaseException
    exc_type: str
    exc_msg: str
    child_traceback: str | None


# meiva:note
# The outcome of one exec as values; the shell executes it (wrap, log, announce). `writes_back`
# is False for a compaction halt: claim wipes the history, so no ToolMessage and no ExecOutput
# follow. `halt_log` words the halt line a lifecycle outcome logs. `boot_failed` marks a child
# that died before the agent's code ran, the operator-visible boot-failure signal.
@dataclass(frozen=True)
class DispatchPlan:
    kind: DispatchKind
    halted: bool
    status: ExecStatus
    body: str
    cancel_reason: InterruptReason = InterruptReason.USER
    writes_back: bool = True
    halt_log: str | None = None
    crash: CrashFacts | None = None
    boot_failed: bool = False

    @property
    def cancelled(self) -> bool:
        return self.kind is DispatchKind.CANCELLED

    @property
    def timed_out(self) -> bool:
        return self.kind is DispatchKind.TIMED_OUT


# meiva:note
# Agent-facing body for a crash whose stdout is empty: the honest replacement for the
# "(no output)" fallback when the crash may have happened BEFORE the agent's code ran (P0 #2100:
# a boot-crash must never read as "ran, produced no output").
#
# `code_reached` is the child's own flag: False = boot-phase crash (the code was NOT executed),
# True = the user-code phase was entered (completion is not implied), None = unknown (older
# child / parent-side construction failure). The type and message name the failure so the agent
# can stop retrying something a retry cannot fix (e.g. a bootstrap fetch failure).
def crashed_no_output_body(crash: CrashFacts, *, code_reached: bool | None) -> str:
    if code_reached is False:
        verdict = "execution failed before running your code — the code was NOT executed"
    elif code_reached is True:
        verdict = "your code may have had effects, but no output was recovered; inspect state before retrying"
    else:
        verdict = (
            "no output was recovered — whether the code executed is unknown; "
            "inspect the current state before retrying"
        )
    return f"[exec crashed: {verdict}]\n{crash.exc_type}: {crash.exc_msg}".rstrip() + "\n"


def _crash_facts(result: ExecCrashed) -> CrashFacts:
    exc = result.exc
    return CrashFacts(
        exc=exc,
        exc_type=getattr(exc, "exc_type", None) or type(exc).__name__,
        exc_msg=getattr(exc, "exc_msg", None) or str(exc),
        child_traceback=result.full_traceback,
    )


# meiva:note
# Map the `ExecResult` sum type to its dispatch plan. Lifecycle priority (lifecycle always wins
# the cancel/timeout race) is decided at the construction site, `construct_exec_result` in
# `_result.py`; this match consumes the sum type directly. Exhaustiveness: pyright strict and
# match narrowing make a forgotten variant a static error.
#
# A compaction appends its note after the agent's own output. A timeout is ordinary feedback,
# not a stop-turn signal: the envelope hints at long-running primitives and the next LLM round
# adapts. A crash's `output` carries the agent-facing traceback; an EMPTY one is never wrapped
# as "(no output)", which would assert the code ran (P0 #2100), so it says what happened.
def plan_dispatch(result: ExecResult) -> DispatchPlan:
    match result:
        case ExecLifecycle(output=output, kind=ExecLifecycleKind.SYSTEM_HALT):
            separator = "" if not output or output.endswith("\n") else "\n"
            return DispatchPlan(
                DispatchKind.COMPACT,
                halted=True,
                status=ExecStatus.HALTED,
                body=output + separator + SYSTEM_HALT_NOTE,
                writes_back=False,
                halt_log="system_halt (compact)",
            )
        case ExecLifecycle(output=output, kind=kind):
            return DispatchPlan(
                DispatchKind.LIFECYCLE,
                halted=True,
                status=ExecStatus.HALTED,
                body=output,
                halt_log=f"lifecycle {kind.value}",
            )
        case ExecCancelled(output=output, reason=reason):
            return DispatchPlan(
                DispatchKind.CANCELLED,
                halted=True,
                status=ExecStatus.CANCELLED,
                body=output,
                cancel_reason=reason,
            )
        case ExecTimedOut(output=output):
            return DispatchPlan(
                DispatchKind.TIMED_OUT, halted=False, status=ExecStatus.TIMED_OUT, body=output
            )
        case ExecCrashed(output=output, code_reached=code_reached):
            crash = _crash_facts(result)
            return DispatchPlan(
                DispatchKind.CRASHED,
                halted=False,
                status=ExecStatus.FAILED,
                body=output or crashed_no_output_body(crash, code_reached=code_reached),
                crash=crash,
                boot_failed=code_reached is False,
            )
        case ExecDone(output=output):
            return DispatchPlan(
                DispatchKind.DONE, halted=False, status=ExecStatus.COMPLETED, body=output
            )
