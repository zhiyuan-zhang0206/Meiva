# meiva:note
# The resident pass loops of the events-maintenance service.
#
# A pass is blocking work handed to a worker thread under a hard deadline. `maintenance_with_liveness`
# runs one and turns an overrun into a permanent failure of the loop's tracker; `pass_loop` repeats
# passes on an interval and follows `pass_policy.verdict` after each. The service's TaskGroup keeps
# every pass proxy, including expired ones, so a late worker failure is still reported with its
# original traceback without restoring health, and stopping the service cancels the proxy while the
# hard exit skips the thread join.
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import assert_never

import psycopg

from base.daemon.loop_health import LoopProgress
from base.daemon.round_loop import RoundFailureBudget
from services.upkeep.events_maintenance import pass_policy
from services.upkeep.events_maintenance.pass_policy import Failure, LogLevel

events_maintenance_log = logging.getLogger("services.upkeep.events_maintenance.daemon")

_LIVENESS_BEAT_STEP_S = 30.0


# meiva:note
# A blocking pass exceeded its deadline and left a worker thread orphaned.
class WedgedPassError(RuntimeError): ...


# meiva:note
# One service-owned worker's terminal result, including a late failure.
class _MaintenancePass:
    def __init__(self, name: str, work: Callable[[], None]) -> None:
        self.name = name
        self.work = work
        self.expired = False

    # meiva:note
    # Return the original failure to the caller, or report an expired pass.
    async def run(self) -> Exception | None:
        try:
            await asyncio.to_thread(self.work)
        except Exception as exc:
            # meiva:note
            # A completed pass's caller owns retry/schema-drift policy. Once its
            # deadline expires that caller parks, so this terminal boundary owns
            # the late traceback. The loop remains permanently unhealthy.
            if self.expired:
                events_maintenance_log.exception(
                    "[events-maintenance] %s pass failed after its hard deadline", self.name
                )
            return exc
        return None


# meiva:note
# Sleep `total_s`, beating progress every `_LIVENESS_BEAT_STEP_S` so the long inter-pass wait keeps
# /healthz fresh instead of reading as a wedged loop.
async def sleep_with_liveness(progress: LoopProgress, total_s: float) -> None:
    remaining = total_s
    while remaining > 0:
        progress.beat()
        step = min(_LIVENESS_BEAT_STEP_S, remaining)
        await asyncio.sleep(step)
        remaining -= step


# meiva:note
# Run one pass (`work`) within the tracker's deadline; unresolved work is not progress. Completion
# beats before propagating its result; a timeout permanently fails the loop.
async def maintenance_with_liveness(
    progress: LoopProgress, work: Callable[[], None], *, tasks: asyncio.TaskGroup
) -> None:
    worker = _MaintenancePass(progress.name, work)
    fut = tasks.create_task(worker.run())
    done, _pending = await asyncio.wait({fut}, timeout=progress.timeout_s)
    if fut not in done:
        worker.expired = True
        message = f"{progress.name} pass exceeded hard deadline of {progress.timeout_s:.1f}s"
        progress.fail(message)
        raise WedgedPassError(message)
    progress.beat()
    failure = fut.result()
    if failure is not None:
        raise failure


def _classify(error: Exception | None) -> Failure | None:
    if error is None:
        return None
    if isinstance(error, WedgedPassError):
        return Failure.WEDGED
    if isinstance(error, psycopg.ProgrammingError):
        return Failure.SCHEMA_DRIFT
    if isinstance(error, psycopg.OperationalError):
        return Failure.DATABASE_UNAVAILABLE
    return Failure.OTHER


def _failure_message(failure: Failure, label: str) -> str:
    match failure:
        case Failure.WEDGED:
            return f"[events-maintenance] {label} pass wedged — parking for watchdog respawn"
        case Failure.SCHEMA_DRIFT:
            return (
                f"[events-maintenance] {label} schema / syntax error — code<->DB drift; "
                "retry will not self-heal, restart after fix"
            )
        case Failure.DATABASE_UNAVAILABLE:
            return f"[events-maintenance] {label} skipped: database unavailable"
        case Failure.OTHER:
            return f"[events-maintenance] {label} iteration failed"
        case _:
            assert_never(failure)


async def _attempt(
    progress: LoopProgress, work: Callable[[], None], tasks: asyncio.TaskGroup
) -> Exception | None:
    try:
        await maintenance_with_liveness(progress, work, tasks=tasks)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return exc
    return None


# meiva:note
# Carry out the verdict for a failed pass: log it at the verdict's level, remember it on the tracker,
# count it against the budget, and end the process when the verdict says retrying cannot help.
def _report(
    plan: pass_policy.Verdict,
    failure: Failure,
    error: Exception,
    *,
    label: str,
    progress: LoopProgress,
    failures: RoundFailureBudget,
) -> None:
    message = _failure_message(failure, label)
    match plan.log:
        case LogLevel.CRITICAL:
            events_maintenance_log.critical(message, exc_info=error)
        case LogLevel.WARNING:
            events_maintenance_log.warning(message, exc_info=error)
        case LogLevel.ERROR:
            events_maintenance_log.error(message, exc_info=error)
        case None:
            pass
        case _:
            assert_never(plan.log)
    if plan.record_error:
        progress.mark_error(str(error))
    if plan.count_failure:
        failures.failed(error)
    if plan.reraise:
        raise error


# meiva:note
# Run `work` immediately, then every `interval_s`. The inter-pass sleep is outside the try, so a
# failed pass waits a full interval before retrying instead of hot-looping against Postgres: the
# passes are idempotent and self-catching-up, so an immediate retry has no value. `label` names the
# loop in its log lines and its failure budget.
async def pass_loop(
    progress: LoopProgress,
    *,
    label: str,
    interval_s: float,
    work: Callable[[], None],
    tasks: asyncio.TaskGroup,
) -> None:
    failures = RoundFailureBudget(f"events-maintenance {label}")
    while True:
        error = await _attempt(progress, work, tasks)
        failure = _classify(error)
        plan = pass_policy.verdict(failure)
        if error is not None and failure is not None:
            _report(plan, failure, error, label=label, progress=progress, failures=failures)
        if plan.park:
            return
        if plan.reset_budget:
            failures.succeeded()
        await sleep_with_liveness(progress, interval_s)
