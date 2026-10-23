# meiva:note
# What a finished `meiva start` owes the boot unit and the boot scheduler.
#
# Both questions are asked of the lifecycle's exit code once it returned: whether the start
# may hand its root to the boot unit, and what a boot-job attempt tells its scheduler. The
# shell (`cli.start_intent`) reads the attempt record and executes the plan.

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "BootDone",
    "BootGaveUp",
    "BootRetry",
    "BootSettlement",
    "HandOffRoot",
    "plan_after_start",
    "plan_boot_settlement",
]


# meiva:note
# Report the root boot complete to the boot unit.
#
# A failed hand-off withdraws the serving marker the start published before it is
# refused, so a start that could not hand its root over never reads as serving.
@dataclass(frozen=True)
class HandOffRoot: ...


# meiva:note
# What a start does once its lifecycle exited with `result`, or None for nothing.
#
# Only a complete success hands the root over: a start that left a service unready
# keeps its nonzero result and the boot log, and the boot unit adopts nothing.
def plan_after_start(result: int) -> HandOffRoot | None:
    return HandOffRoot() if result == 0 else None


# meiva:note
# The attempt succeeded: its record is cleared and the job exits 0.
@dataclass(frozen=True)
class BootDone: ...


# meiva:note
# The attempt failed with budget left: the job exits with the start's own code.
@dataclass(frozen=True)
class BootRetry:
    exit_code: int


# meiva:note
# The failing attempt spent the budget: alert, say why, and stop the scheduler.
@dataclass(frozen=True)
class BootGaveUp:
    attempt: int
    failed_code: int
    message: str


BootSettlement = BootDone | BootRetry | BootGaveUp


# meiva:note
# What the boot-job attempt number `attempt` that exited with `code` tells its scheduler.
#
# `spent` is whether this attempt is the last the policy allows
# (`base.host.system.boot_policy.budget_spent`). The count never refuses a start; it only
# decides what a failed attempt asks of the scheduler.
def plan_boot_settlement(
    code: int, *, attempt: int, max_attempts: int, spent: bool
) -> BootSettlement:
    if code == 0:
        return BootDone()
    if not spent:
        return BootRetry(code)
    return BootGaveUp(
        attempt=attempt,
        failed_code=code,
        message=(
            f"meiva start --boot: attempt {attempt} of {max_attempts} failed "
            f"(exit {code}); not retrying until an operator runs `meiva start`"
        ),
    )
