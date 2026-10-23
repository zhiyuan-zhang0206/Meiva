# meiva:note
# What the interrupt watcher decides, as values: whether a poll result fires the event, who
# reports a watcher failure, what a watcher's fate reads as, and how a bounded exit ends. The
# shell in `_watch.py` runs the loop, the task and the logging; every branch that picks between
# outcomes lives here so a test asserts the verdict instead of observing a spy.
# meiva:link agent/graph/docs/interrupt-ownership.meiva.okf.md

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum

from base.agents.messages.inbound import InterruptReason

# meiva:note
# How long exit waits for the cancelled watcher to unwind. Cancellation lands instantly on a
# healthy await, but a watcher blocked on a wedged DB call (a frozen Postgres host, a network
# blip) can sit for up to the kernel's TCP retry budget (~2 min); an unbounded wait would then
# freeze the whole turn after the model already finished. The watcher does nothing after cancel
# besides unwinding, and its original service still owns the task and its eventual result.
WATCHER_EXIT_TIMEOUT_S = 5.0


class WatchStep(Enum):
    EXIT = "exit"
    WAIT = "wait"


@dataclass(frozen=True)
class Fire:
    reason: InterruptReason


# meiva:note
# One pass of the poll loop. A reason found while `stop` is set never fires: the lost-cancel race
# can leave a survivor whose poll returns after the node's turn ended, and a spurious interrupt
# would then abort the next action.
def watch_step(reason: InterruptReason | None, *, stopped: bool) -> Fire | WatchStep:
    if stopped:
        return WatchStep.EXIT
    if reason is not None:
        return Fire(reason)
    return WatchStep.WAIT


class FailureAction(Enum):
    REPORT_TO_SERVICE = "report_to_service"
    WARN_AND_INTERRUPT = "warn_and_interrupt"
    WARN = "warn"


# meiva:note
# A watcher that failed after the invocation handed it off belongs to its service, which raises
# it at stop/join. A failure inside the invocation aborts the node it guards (a blind node would
# run uninterruptibly) unless the invocation is already over.
def failure_action(*, late: bool, stopped: bool) -> FailureAction:
    if late:
        return FailureAction.REPORT_TO_SERVICE
    if stopped:
        return FailureAction.WARN
    return FailureAction.WARN_AND_INTERRUPT


def watcher_fate(error: BaseException | None, *, cancelled: bool) -> str:
    if error is not None:
        return f"raised {error!r}"
    return "cancelled" if cancelled else "returned normally"


@dataclass(frozen=True)
class ExitVerdict:
    retain: bool
    raised: BaseException | None
    cause: BaseExceptionGroup[BaseException] | None
    note: str | None


# meiva:note
# How the bounded exit of one watcher ends. A watcher still running at the deadline is retained
# by its service (`retain`). A watcher that unwound with an error surfaces it: alone when nothing
# else is in flight, otherwise beside the primary failure or the caller's own cancellation, which
# keeps its identity and carries the watcher error as a note and a grouped cause. A caller
# cancelled during the wait is cancelled once the wait ends, unless a primary failure outranks it.
def exit_verdict(
    *,
    unwound: bool,
    error: BaseException | None,
    primary: BaseException | None,
    cancellation: asyncio.CancelledError | None,
) -> ExitVerdict:
    if unwound and error is not None:
        outer = primary if primary is not None else cancellation
        if outer is None:
            return ExitVerdict(retain=False, raised=error, cause=None, note=None)
        secondary: list[BaseException] = [error]
        if outer.__cause__ is not None:
            secondary.insert(0, outer.__cause__)
        return ExitVerdict(
            retain=False,
            raised=outer,
            cause=BaseExceptionGroup("interrupt cleanup failures", secondary),
            note=f"interrupt watcher cleanup also failed: {error!r}",
        )
    return ExitVerdict(
        retain=not unwound,
        raised=cancellation if primary is None else None,
        cause=None,
        note=None,
    )
