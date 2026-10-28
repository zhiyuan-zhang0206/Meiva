from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProcessObservation:
    pid: int
    command: str


# meiva:note
# What `interrupt()` answers, for every adapter. `settled` is None when the runtime pid is
# unobservable (an unobservable tree is reported as unknown, never as clean), and `remaining` names
# every process that outlived the sweep so the caller decides instead of assuming. `killed` lists
# the pids an adapter SIGKILLed after the CLI exited on SIGINT without its tool tree (only the
# Antigravity transport reaps that way).
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
@dataclass(frozen=True, slots=True)
class CancelReport:
    turn_id: str | None
    interrupt_ms: float
    settled: bool | None
    observed: tuple[ProcessObservation, ...]
    remaining: tuple[ProcessObservation, ...]
    waited_ms: float
    cli_pid: int | None
    cli_alive: bool | None
    killed: tuple[int, ...] = ()


def describe(observed: Mapping[int, str], pids: Iterable[int]) -> tuple[ProcessObservation, ...]:
    return tuple(ProcessObservation(pid=pid, command=observed.get(pid, "")) for pid in sorted(pids))


# meiva:note
# The descendants a cancel is answerable for: those seen now that were not there when the turn
# began.
def spawned_since(observed: Mapping[int, str], baseline: Mapping[int, str]) -> dict[int, str]:
    return {child: command for child, command in observed.items() if child not in baseline}


# meiva:note
# A cancel that could not look at a process tree: no runtime pid, no transport, or nothing running.
# Nothing is claimed about the tree, so `settled` is None; `cli_pid` and `cli_alive` carry what is
# known about the CLI itself (nothing, or a healthy process that was left alone).
def unobserved_cancel(
    *,
    turn_id: str | None,
    interrupt_ms: float = 0.0,
    cli_pid: int | None = None,
    cli_alive: bool | None = None,
) -> CancelReport:
    return CancelReport(
        turn_id=turn_id,
        interrupt_ms=interrupt_ms,
        settled=None,
        observed=(),
        remaining=(),
        waited_ms=0.0,
        cli_pid=cli_pid,
        cli_alive=cli_alive,
    )


# meiva:note
# What a sweep of the tree saw: the whole tree at the start (`observed`), the descendants the turn
# started (`spawned`), whether the ones waited on are gone (`gone`) after `waited_ms`, and which of
# them are still running when the report is cut (`alive`; empty is a clean sweep). The adapter
# reads these from its probe; the report is decided from them here.
@dataclass(frozen=True, slots=True)
class Sweep:
    observed: Mapping[int, str]
    spawned: Mapping[int, str]
    gone: bool
    waited_ms: float
    alive: Collection[int]


# meiva:note
# A cancel that watched the tree: `settled` is whether the sweep found everything gone, `observed`
# names every process the turn started and `remaining` those of them that outlived it.
def observed_cancel(
    *,
    turn_id: str | None,
    interrupt_ms: float,
    pid: int,
    cli_alive: bool,
    sweep: Sweep,
    killed: tuple[int, ...] = (),
) -> CancelReport:
    return CancelReport(
        turn_id=turn_id,
        interrupt_ms=interrupt_ms,
        settled=sweep.gone,
        observed=describe(sweep.observed, sweep.spawned),
        remaining=describe(sweep.observed, sweep.alive),
        waited_ms=sweep.waited_ms,
        cli_pid=pid,
        cli_alive=cli_alive,
        killed=killed,
    )
