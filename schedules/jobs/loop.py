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
from datetime import UTC, datetime
from typing import NoReturn, assert_never

from base.daemon.schedules.watcher import next_fire
from base.db import Database
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
# `started_at` is the first floor: a slot at or before the process's start is never fired, so a
# restart does not replay what the previous process already handled or missed.
def run_loop(
    db: Database,
    schedule: CronSchedule,
    fire: Callable[[datetime, str | None], object],
    *,
    schedule_id: int,
    started_at: datetime,
) -> NoReturn:
    floor = started_at
    while True:
        tick = loop_once(
            db, schedule, fire, floor=floor, now=datetime.now(UTC), schedule_id=schedule_id
        )
        floor = tick.floor
        time.sleep(tick.sleep)
