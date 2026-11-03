# meiva:note
# The loop every built-in schedule runs: ask the plan what is due, claim it, sleep.
#
# One iteration is `loop_once`: it computes each trigger's next slot, lets `plan_step` pick
# between sleeping and claiming, and claims through `fire_slot_once` when a slot is due. It
# returns how long to sleep and the floor to use next, so a test drives it with a chosen `now`
# against a real database. `run_loop` is `loop_once` forever and owns the only sleep and the
# only clock read.
# meiva:link schedules/README.md

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import NoReturn, assert_never

from base.daemon.schedules.heartbeat import write_beat
from base.daemon.schedules.watcher import next_fire
from base.db import Database
from base.deploy.schedule_timing import SCHEDULE_FIRST_ROUND_BUDGET_S
from schedules.jobs.catchup import Fired, fire_slot_once
from schedules.plans.loop_plan import Fire, LoopTiming, Sleep, Upcoming, plan_step, search_base

__all__ = ["CronSchedule", "LoopTick", "Trigger", "loop_once", "run_loop"]


# meiva:note
# One cron expression and the payload its claim hands to the fire callback.
@dataclass(frozen=True)
class Trigger:
    cron: str
    payload: str | None = None


# meiva:note
# What a loop waits for: its triggers, the cluster timezone every cron is read in, and its
# timing. `timezone` is the entry's `cluster_timezone()`; the host's own zone never enters.
@dataclass(frozen=True)
class CronSchedule:
    triggers: tuple[Trigger, ...]
    timezone: str
    timing: LoopTiming = field(default_factory=LoopTiming)


# meiva:note
# One iteration's outcome. `sleep` is the pause before the next one, `floor` the search floor
# to carry, and `fired` the claimed slot with its callback's result (`None` when nothing was
# due or another runner won the claim).
@dataclass(frozen=True)
class LoopTick[R]:
    sleep: float
    floor: datetime
    fired: Fired[R] | None


def loop_once[R](
    db: Database,
    schedule: CronSchedule,
    fire: Callable[[datetime, str | None], R],
    *,
    floor: datetime,
    now: datetime,
    schedule_id: int,
) -> LoopTick[R]:
    base = search_base(floor, now, schedule.timing)
    upcoming = tuple(
        Upcoming(next_fire(item.cron, after=base, timezone=schedule.timezone), item.payload)
        for item in schedule.triggers
    )
    step = plan_step(upcoming, now, schedule.timing)
    match step:
        case Sleep(seconds=seconds):
            return LoopTick(seconds, floor, None)
        case Fire(slot=slot, trigger=trigger, rest=rest):
            fired = fire_slot_once(db, slot, trigger, fire=fire, schedule_id=schedule_id)
            return LoopTick(rest, max(floor, slot), fired)
        case _:
            assert_never(step)


# meiva:note
# A resident loop writes the heartbeat the schedule manager already reads, because it is the only
# layer that knows a round from a restart: without it a loop that stopped iterating looks exactly
# like one with nothing to do, and the one remaining signal is the two-hour sessionless clock. Left
# to each schedule script the mechanism goes unused -- of the built-in schedules only the worktree
# collector ever wrote a beat -- so the loop writes it for every schedule that runs through here.
#
# `home` defaults to None for one reason: a schedule row stores its own copy of the script, and the
# copy in the table predates this parameter until `meiva schedules provision` (or the next manager
# start) refreshes it. A stored copy that omits it must keep running exactly as before -- the
# pre-wave `down` verify binds those stored calls against this signature, and a required argument
# would refuse the whole cluster's update. Every template in this repository passes it.
def _write_round_beat(
    home: Path | None, schedule_id: int, *, now: datetime, sleep_s: float, budget_s: float
) -> None:
    if home is None:
        return
    promise = now + timedelta(seconds=sleep_s + budget_s)
    write_beat(home, schedule_id, written_at=now, alive_by=promise, budget_s=budget_s)


# meiva:note
# The promise is "the next beat lands by now + sleep + the slowest round this loop has taken". It
# starts from a budget the schedule does not have to declare and tightens itself as rounds are
# measured; a round that hangs never raises the bar, which is what makes the promise worth reading.
def _next_round_budget(previous_s: float, round_s: float) -> float:
    return max(previous_s, round_s)


# meiva:note
# `started_at` is the first floor: a slot at or before the process's start is never fired, so a
# restart does not replay what the previous process already handled or missed.
def run_loop(
    db: Database,
    schedule: CronSchedule,
    fire: Callable[[datetime, str | None], object],
    *,
    schedule_id: int,
    started_at: datetime,
    clock: Callable[[], datetime],
    home: Path | None = None,
) -> NoReturn:
    floor = started_at
    budget = SCHEDULE_FIRST_ROUND_BUDGET_S
    _write_round_beat(home, schedule_id, now=clock(), sleep_s=0.0, budget_s=budget)
    while True:
        began = clock()
        tick = loop_once(db, schedule, fire, floor=floor, now=began, schedule_id=schedule_id)
        floor = tick.floor
        ended = clock()
        budget = _next_round_budget(budget, (ended - began).total_seconds())
        _write_round_beat(home, schedule_id, now=ended, sleep_s=tick.sleep, budget_s=budget)
        time.sleep(tick.sleep)
