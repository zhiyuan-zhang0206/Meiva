"""Exec outcome sum type — the mutually exclusive result variants one
`execute_code` run produces, plus the priority constructor and the
parent-side placeholder for a crash that happened in a child process.

Moved out of `agent/graph/exec/node.py` (2026-08, exec-subprocess work) so the
subprocess machinery (`agent/graph/exec/_subprocess.py`) can construct the
same sum type the exec node dispatches without importing `node.py` (which
would close an import cycle once `node.py` itself imports the machinery).
`node.py` re-exports every name here, so existing callers and tests keep
their imports.

`ExecChildError` is the one variant carrier: a real exception cannot
cross a process boundary, so a crashed child ships `exc_type` / `exc_msg` /
`full_traceback` as strings in the result envelope, and the parent wraps them
in this Exception subclass. `ExecCrashed.full_traceback` (optional) carries
the child-formatted text; when it is None (a parent-side construction failure
— spawn error, unserializable state) the dispatcher falls back to
`format_full_traceback(exc)`.

Every variant carries `stream_cap` (set when the accumulation budget dropped
the middle of `output` mid-run) so both execution paths hand the cap to the
envelope uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from base.agents.messages.inbound import InterruptReason


class ExecChildError(Exception):
    """A crash inside the exec child, re-raised parent-side with the child's
    traceback text. The child's real exception object cannot cross the process
    boundary; its type, message, and formatted traceback ride the result
    envelope instead."""

    def __init__(self, exc_type: str, exc_msg: str, full_traceback: str | None) -> None:
        self.exc_type = exc_type
        self.exc_msg = exc_msg
        self.full_traceback = full_traceback
        super().__init__(exc_msg or exc_type)


# meiva:note
# Record that the accumulation budget dropped the middle of an exec's output. It travels with
# the text through the `ExecResult` sum type into `output.wrap_code_output`, which needs both
# numbers to stay honest: `produced_chars` is the TRUE pre-truncation length the
# instrumentation log line reports, and the overflow archive can no longer claim to hold the
# full output — the middle is gone before the archive is ever written.
@dataclass(frozen=True)
class StreamCap:
    produced_chars: int
    budget_chars: int


# meiva:note
# The framework lifecycle signals agent code can raise. The values are the class names the child
# reports in its result envelope, so the closed enum is the only vocabulary the parent and the
# dispatcher share and no module here imports the exception classes.
class LifecycleKind(Enum):
    SYSTEM_HALT = "SystemHalt"
    TERMINATION = "AgentTermination"
    RESTART = "AgentRestart"


@dataclass(frozen=True)
class ExecDone:
    """Execution completed, no exception."""

    output: str
    stream_cap: StreamCap | None = None


@dataclass(frozen=True)
class ExecCancelled:
    """External abort -> child interruption and owned-resource cleanup;
    `output` contains accumulated partial output + KeyboardInterrupt traceback."""

    output: str
    stream_cap: StreamCap | None = None
    reason: InterruptReason = InterruptReason.USER


@dataclass(frozen=True)
class ExecTimedOut:
    """Deadline -> child got SIGTERM -> TimeoutError -> exit (or was SIGKILLed
    past the grace period)."""

    output: str
    stream_cap: StreamCap | None = None


# meiva:note
# Agent actively called `meiva.self.{terminate,restart,compact}`, raising a `LifecycleExit`
# subclass inside the child — lifecycle takes priority over cancel/timeout. The kind is rebuilt
# parent-side from the envelope's `lifecycle_type` name.
@dataclass(frozen=True)
class ExecLifecycle:
    output: str
    kind: LifecycleKind
    stream_cap: StreamCap | None = None


@dataclass(frozen=True)
class ExecCrashed:
    """Non-lifecycle exception (SyntaxError / NameError / missing import /
    SystemExit subclasses / user code raising KeyboardInterrupt|TimeoutError,
    etc.). Traceback is already in output; exec_node logs at INFO +
    event=exec_failed. When the crash happened in the exec child,
    `full_traceback` carries the child-formatted text (the `exc` is an
    `ExecChildError` placeholder); None means a parent-side construction
    failure and the dispatcher formats from `exc`."""

    output: str
    exc: BaseException
    full_traceback: str | None = None
    # False = the child died before reaching the agent's code (boot-phase
    # crash); True = the code ran; None = unknown (older child / parent-side
    # construction failure). With empty output, False lets the dispatcher say
    # "the code was NOT executed" instead of the "(no output)" lie (P0 #2100).
    code_reached: bool | None = None
    stream_cap: StreamCap | None = None


type ExecResult = ExecDone | ExecCancelled | ExecTimedOut | ExecLifecycle | ExecCrashed


# meiva:note
# The lifecycle kind a child reported by class name, or None for an unknown class so the
# subprocess parent can surface a protocol crash.
def lifecycle_kind_from_name(name: str) -> LifecycleKind | None:
    try:
        return LifecycleKind(name)
    except ValueError:
        return None


# meiva:note
# Dispatch priority: lifecycle > cancel > timeout > crashed > done.
#
# Lifecycle always wins (the agent actively calling terminate/restart/compact has higher
# semantic priority than "was interrupted"; SDK already INSERTed inbound). Cancel > timeout
# (CLAUDE design: cancel always wins).
#
# `code_reached` rides the crash variant only (see `ExecCrashed`): the child's envelope reports
# whether the agent-authored code ever started.
def construct_exec_result(
    output: str,
    outcome: LifecycleKind | BaseException | None,
    *,
    cancelled: bool,
    timed_out: bool,
    stream_cap: StreamCap | None = None,
    code_reached: bool | None = None,
) -> ExecResult:
    if isinstance(outcome, LifecycleKind):
        return ExecLifecycle(output=output, kind=outcome, stream_cap=stream_cap)
    if cancelled:
        return ExecCancelled(output=output, stream_cap=stream_cap)
    if timed_out:
        return ExecTimedOut(output=output, stream_cap=stream_cap)
    if outcome is not None:
        return ExecCrashed(
            output=output, exc=outcome, stream_cap=stream_cap, code_reached=code_reached
        )
    return ExecDone(output=output, stream_cap=stream_cap)
