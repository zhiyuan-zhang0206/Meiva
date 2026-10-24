# meiva:note
# Whether and how the standalone launchd Redis bridge is retired.
#
# An earlier layout ran the relay as its own LaunchAgent; the root now supervises it, so converge
# unloads that job and removes its plist and installed copy, leaving exactly one supervisor of
# the listener. The shell asks launchd and the filesystem what is there and executes the plan;
# the wait for the unload is a bounded poll whose verdicts are values.

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "Expired",
    "Leave",
    "Retire",
    "RetirementPlan",
    "Retry",
    "WaitStep",
    "bootout_wait_step",
    "plan_retirement",
    "still_loaded_message",
]


# meiva:note
# Nothing of the standalone bridge is left: the job is not loaded and no file remains.
@dataclass(frozen=True)
class Leave: ...


# meiva:note
# Remove the plist and the installed copy; unload the job first when it is loaded.
@dataclass(frozen=True)
class Retire:
    unload: bool


RetirementPlan = Leave | Retire


# meiva:note
# What is left of the standalone bridge decides what retirement does.
def plan_retirement(
    *, loaded: bool, plist_present: bool, installed_present: bool
) -> RetirementPlan:
    if not (loaded or plist_present or installed_present):
        return Leave()
    return Retire(unload=loaded)


# meiva:note
# Ask launchd again after `after_s` seconds.
@dataclass(frozen=True)
class Retry:
    after_s: float


# meiva:note
# The job stayed loaded for the whole budget.
@dataclass(frozen=True)
class Expired: ...


WaitStep = Retry | Expired


# meiva:note
# The next step of the unload wait after a reading that still showed the job loaded.
#
# The wait reads first and asks this afterwards, so a job that is already gone settles even
# when the budget is spent.
def bootout_wait_step(*, elapsed_s: float, timeout_s: float, interval_s: float) -> WaitStep:
    if elapsed_s >= timeout_s:
        return Expired()
    return Retry(interval_s)


# meiva:note
# The refusal for a job that would not unload: the files are kept so an operator can inspect.
def still_loaded_message(*, label: str, timeout_s: float, plist_path: str) -> str:
    return (
        f"Redis bridge LaunchAgent {label} remained loaded after "
        f"{timeout_s:.0f}s; retained {plist_path} for inspection"
    )
