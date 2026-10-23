# meiva:note
# What a stop does once the pty-sessions closure has returned its outcome.
#
# The closure signals the shells and reports the known processes that outlived it. The shell
# observes which of them are still alive and asks `plan_after_closure` what that means: write the
# owners' notices first (the service no longer lists a closed session, so a retry could never
# record it), say which known jobs are still running, and either fail on a surviving shell or wait
# for the service's table to empty.

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "AwaitNoSessions",
    "ClosureStep",
    "FailShells",
    "ReportJobLeftover",
    "SurvivorFact",
    "WriteNotices",
    "forced_failure_message",
    "job_leftover_line",
    "plan_after_closure",
    "shells_alive_header",
]


# meiva:note
# One known process the closure reported, and whether it is still its recorded process.
#
# `role` is "shell" (the shell itself) or "job" (any other recorded member).
@dataclass(frozen=True)
class SurvivorFact:
    session: str
    role: str
    pid: int
    alive: bool


# meiva:note
# Write one notice per closed busy session to its owner agent.
@dataclass(frozen=True)
class WriteNotices: ...


# meiva:note
# Say that a known job of a closed session is still running (a diagnostic, never a gate).
@dataclass(frozen=True)
class ReportJobLeftover:
    session: str
    pid: int


# meiva:note
# Fail the stop: these known shells are still alive after the SIGKILL leg.
@dataclass(frozen=True)
class FailShells:
    sessions: tuple[str, ...]
    pids: tuple[int, ...]


# meiva:note
# Wait for the service's table to empty; a session born during the closure fails it.
@dataclass(frozen=True)
class AwaitNoSessions: ...


ClosureStep = WriteNotices | ReportJobLeftover | FailShells | AwaitNoSessions


# meiva:note
# The steps after a closure, in order.
#
# A normal closure notifies owners and waits for the table to empty; a forced one does
# neither. A surviving shell fails both; a known job leftover is only reported.
def plan_after_closure(
    survivors: tuple[SurvivorFact, ...], *, forced: bool
) -> tuple[ClosureStep, ...]:
    steps: list[ClosureStep] = [] if forced else [WriteNotices()]
    steps.extend(
        ReportJobLeftover(fact.session, fact.pid)
        for fact in survivors
        if fact.role != "shell" and fact.alive
    )
    shells = sorted(
        (fact for fact in survivors if fact.role == "shell" and fact.alive),
        key=lambda fact: fact.pid,
    )
    if shells:
        steps.append(
            FailShells(
                tuple(sorted({fact.session for fact in shells})), tuple(f.pid for f in shells)
            )
        )
    elif not forced:
        steps.append(AwaitNoSessions())
    return tuple(steps)


def job_leftover_line(step: ReportJobLeftover) -> str:
    return (
        f"shell-session closure {step.session!r}: known job still running: "
        f"pid={step.pid}; inspect the process before taking action"
    )


# meiva:note
# The first line of the failure a normal stop raises (the inventory follows it).
def shells_alive_header(step: FailShells) -> str:
    return (
        f"shell-session closure incomplete — known shells remain alive: "
        f"{list(step.pids)} from sessions: {list(step.sessions)}"
    )


def forced_failure_message(step: FailShells) -> str:
    return f"force stop did not close shell sessions: {list(step.sessions)}"
