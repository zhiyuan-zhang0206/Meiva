# meiva:note
# The batch's calls on its workers: read each by id, wait for them, terminate the ones to give
# up on, and clear out leftovers of an earlier batch.
#
# Known workers are read with `get_status` and `get_last_message` by id and never by listing the
# directory. `plan_wait` decides after every poll; this module sleeps and terminates as it says.
# meiva:link schedules/docs/adversarial-eval.meiva.okf.md

from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import assert_never

import meiva
from meiva.agents import AgentStatus
from schedules.jobs.agent_directory import iter_agents
from schedules.plans.adversarial_workers import (
    GiveUp,
    Poll,
    Settled,
    WorkerPhase,
    WorkerRead,
    child_spawners,
    leftover_workers,
    live_workers,
    plan_wait,
)
from schedules.plans.adversarial_spawn import COLLEAGUE_LABEL, PROBE_LABEL
from schedules.plans.directory_plan import ListedAgent

__all__ = [
    "BATCH_DEADLINE",
    "POLL_SECONDS",
    "child_spawners_of",
    "sweep_leftover_workers",
    "terminate_live",
    "wait_for_workers",
]

POLL_SECONDS = 30.0
BATCH_DEADLINE = timedelta(minutes=25)


def _phase(status: AgentStatus) -> WorkerPhase:
    match status:
        case AgentStatus.TERMINATED:
            return WorkerPhase.TERMINATED
        case AgentStatus.IDLING:
            return WorkerPhase.IDLING
        case AgentStatus.RUNNING:
            return WorkerPhase.ACTIVE
        case _:
            assert_never(status)


def _read_worker(agent_id: int) -> WorkerRead:
    phase = _phase(meiva.agents.get_status(agent_id))
    has_output = phase is WorkerPhase.IDLING and bool(meiva.agents.get_last_message(agent_id))
    return WorkerRead(phase, has_output)


# meiva:note
# Returns the workers that did not finish within `deadline` after being terminated with force.
# The deadline is measured from the call; `poll_seconds` is the pause between reads.
def wait_for_workers(
    worker_ids: Iterable[int], deadline: timedelta, poll_seconds: float
) -> frozenset[int]:
    pending = frozenset(worker_ids)
    give_up_at = datetime.now(UTC) + deadline
    while True:
        reads = {agent_id: _read_worker(agent_id) for agent_id in sorted(pending)}
        step = plan_wait(reads, datetime.now(UTC), give_up_at, poll_seconds)
        match step:
            case Settled():
                return frozenset()
            case Poll(pending=still_pending, seconds=seconds):
                pending = still_pending
                time.sleep(seconds)
            case GiveUp(pending=left):
                for agent_id in sorted(left):
                    meiva.agents.terminate(agent_id, force=True)
                return left
            case _:
                assert_never(step)


def terminate_live(agent_ids: Iterable[int]) -> None:
    phases = {agent_id: _phase(meiva.agents.get_status(agent_id)) for agent_id in agent_ids}
    for agent_id in live_workers(phases):
        meiva.agents.terminate(agent_id, force=True)


def sweep_leftover_workers(current: frozenset[int]) -> None:
    for label in (PROBE_LABEL, COLLEAGUE_LABEL):
        listed = tuple(
            ListedAgent(row.agent_id, row.label, row.status is AgentStatus.TERMINATED)
            for row in iter_agents(scope="live", query=label)
        )
        for agent_id in leftover_workers(label, listed, current):
            meiva.agents.terminate(agent_id, force=True)


def child_spawners_of(worker_ids: Iterable[int]) -> tuple[str, ...]:
    spawners = (row.spawner for row in iter_agents(scope="all", limit=200))
    return child_spawners(spawners, worker_ids)
