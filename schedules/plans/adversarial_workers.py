# meiva:note
# Which of the batch's workers are done, which to give up on, and which agents to terminate.
#
# The shell reads each worker's phase (and, for an idle one, whether it has produced output) and
# asks this module what to do next; it sleeps, terminates and polls again as told. Known workers
# are read by id and never found by enumerating the directory; only leftovers from an earlier
# batch are searched for, by their labels, among live agents.
# meiva:link schedules/docs/adversarial-eval.meiva.okf.md

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import assert_never

from schedules.plans.directory_plan import ListedAgent

__all__ = [
    "GiveUp",
    "Poll",
    "Settled",
    "WaitStep",
    "WorkerPhase",
    "WorkerRead",
    "child_spawners",
    "leftover_workers",
    "live_workers",
    "plan_wait",
    "worker_done",
]


class WorkerPhase(Enum):
    TERMINATED = "terminated"
    IDLING = "idling"
    ACTIVE = "active"


# meiva:note
# One worker as the shell found it. `has_output` is read only for an idling worker (its latest
# turn text is not empty) and is false for every other phase.
@dataclass(frozen=True)
class WorkerRead:
    phase: WorkerPhase
    has_output: bool


@dataclass(frozen=True)
class Settled:
    pass


# meiva:note
# Check `pending` again after `seconds`.
@dataclass(frozen=True)
class Poll:
    pending: frozenset[int]
    seconds: float


# meiva:note
# The deadline passed with `pending` still working: terminate them and stop waiting.
@dataclass(frozen=True)
class GiveUp:
    pending: frozenset[int]


WaitStep = Settled | Poll | GiveUp


# meiva:note
# IDLING counts as done only with output already produced: a freshly spawned worker's row
# starts as IDLING before its process boots, so bare IDLING read the whole batch as
# finished seconds after spawning and audited empty transcripts (adversarial-eval
# 2026-35, 2026-08-26). A worker that finished but failed to self-terminate still ends a
# turn IDLING with the report message the audit reads.
# meiva:link schedules/docs/adversarial-eval.meiva.okf.md
def worker_done(read: WorkerRead) -> bool:
    match read.phase:
        case WorkerPhase.TERMINATED:
            return True
        case WorkerPhase.IDLING:
            return read.has_output
        case WorkerPhase.ACTIVE:
            return False
        case _:
            assert_never(read.phase)


# meiva:note
# `reads` holds the workers still pending at this poll; a worker that is done never comes back.
# Once the deadline has passed, whatever is still not done is given up on, after one last read
# of every worker so a worker that finished during the final pause is not terminated.
def plan_wait(
    reads: Mapping[int, WorkerRead], now: datetime, deadline: datetime, poll_seconds: float
) -> WaitStep:
    pending = frozenset(agent_id for agent_id, read in reads.items() if not worker_done(read))
    if not pending:
        return Settled()
    if now >= deadline:
        return GiveUp(pending)
    return Poll(pending, poll_seconds)


# meiva:note
# The agents of an aborted batch that are still not terminated, lowest id first.
def live_workers(phases: Mapping[int, WorkerPhase]) -> tuple[int, ...]:
    return tuple(
        sorted(
            agent_id for agent_id, phase in phases.items() if phase is not WorkerPhase.TERMINATED
        )
    )


# meiva:note
# Workers of an earlier batch: live agents carrying exactly the worker label (the directory
# search matches substrings) that the current batch did not spawn.
def leftover_workers(
    label: str, agents: Sequence[ListedAgent], current: frozenset[int]
) -> tuple[int, ...]:
    return tuple(
        agent.agent_id
        for agent in agents
        if agent.label == label and not agent.terminated and agent.agent_id not in current
    )


# meiva:note
# The spawner values that name a worker of this batch as the spawning agent, sorted. A probe that
# spawned a child (the c007 bait) leaves a row whose spawner is `agent:<probe id>`.
def child_spawners(spawners: Iterable[str], worker_ids: Iterable[int]) -> tuple[str, ...]:
    wanted = {f"agent:{agent_id}" for agent_id in worker_ids}
    return tuple(sorted({spawner for spawner in spawners if spawner in wanted}))
