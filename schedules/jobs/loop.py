# meiva:note
# The loop every built-in schedule runs: ask the plan what is due, claim it, sleep.
#
# One iteration is `loop_once`: it computes each trigger's next slot, lets `plan_step` pick
# between sleeping and claiming, and claims through `fire_slot_once` when a slot is due. It
# returns how long to sleep and the floor to use next, so a test drives it with a chosen `now`
# against a real database. `run_loop` is `loop_once` forever and owns the only sleep and the
# only clock read. A round that raises does not end the loop: it backs off and retries, because
# this process is the only thing that can move its schedule forward.
# meiva:link schedules/README.md

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NoReturn, assert_never

from base.daemon.schedules.heartbeat import write_round_beat
from base.daemon.schedules.watcher import next_fire
from base.db import Database
from base.deploy.schedule_timing import SCHEDULE_FIRST_ROUND_BUDGET_S
from schedules.jobs.catchup import Fired, fire_slot_once
from schedules.plans.loop_plan import (
    FailurePolicy,
    Fire,
    LoopTiming,
    Sleep,
    Upcoming,
    failure_backoff_s,
    plan_step,
    search_base,
)

__all__ = [
    "CronSchedule",
    "LoopTick",
    "Trigger",
    "loop_once",
    "run_loop",
    "run_rounds",
]

_log = logging.getLogger(__name__)


# meiva:note
# One cron expression and the payload its claim hands to the fire callback.
@dataclass(frozen=True)
class Trigger:
    cron: str
    payload: str | None = None


# meiva:note
# What a loop waits for: its triggers, the cluster timezone every cron is read in, its timing, and
# what it does when a round raises. `timezone` is the entry's `cluster_timezone()`; the host's own
# zone never enters. `failure` is last so a schedule that overrode only its timing keeps its
# positional arguments.
@dataclass(frozen=True)
class CronSchedule:
    triggers: tuple[Trigger, ...]
    timezone: str
    timing: LoopTiming = field(default_factory=LoopTiming)
    failure: FailurePolicy = field(default_factory=FailurePolicy)


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
# meiva:note
# The promise is "the next beat lands by now + sleep + the slowest round this loop has taken". It
# starts from a budget the schedule does not have to declare and tightens itself as rounds are
# measured; a round that hangs never raises the bar, which is what makes the promise worth reading.
def _next_round_budget(previous_s: float, round_s: float) -> float:
    return max(previous_s, round_s)


# meiva:note
# One failed round is a warning; the round that reaches the policy's report threshold is an error
# carrying the traceback, and it is also the one that lands in the schedule row. Both are written
# for whoever reads the schedule's log afterwards -- and, unlike the failure mode this replaces,
# the loop is still running to write the next line.
def _report_round_failure(
    db: Database,
    policy: FailurePolicy,
    schedule_id: int,
    failures: int,
    pause: float,
    exc: Exception,
) -> None:
    _log.warning(
        "schedule %s round failed (%s: %s); %d in a row, retrying in %.0fs",
        schedule_id,
        type(exc).__name__,
        exc,
        failures,
        pause,
    )
    if failures != policy.report_after:
        return
    _log.error(
        "schedule %s has failed %d rounds in a row; retrying every %.0fs until it recovers",
        schedule_id,
        failures,
        pause,
        exc_info=exc,
    )
    _write_last_error(
        db,
        schedule_id,
        f"round failed {failures} times in a row (next attempt in {pause:.0f}s): "
        f"{type(exc).__name__}: {exc}",
    )


# meiva:note
# `schedules.last_error` is where a person looks -- `meiva schedules get` prints it, and the
# mutating commands echo the row they changed; `meiva schedules ls` shows only the summary columns,
# so the reason is one `get` away rather than in the list -- and it is where the previous failure
# mode wrote by accident: a round that raised killed the process,
# and the manager recorded the loss in this column. A loop that survives the same round is the
# quieter outcome, so it fills the column itself; otherwise the only trace is one line in a
# session log nobody reads, which is not a signal.
#
# Best effort by design: a loop that dies on its own bookkeeping is worse than a quiet one, and
# the database being unreachable is already the failure being reported here.
def _write_last_error(db: Database, schedule_id: int, message: str | None) -> None:
    try:
        with db.write_transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE schedules SET last_error = %s, updated_at = now() WHERE id = %s",
                (message, schedule_id),
            )
    except Exception as exc:
        _log.warning("schedule %s could not record its round failure: %s", schedule_id, exc)


# meiva:note
# The guarded driver every resident schedule loop runs: one round, its beat, its sleep, forever.
#
# A round is `advance`, and it is called with the instant it starts and the state the previous
# round returned. It answers the state to carry into the next round and how long to sleep. That is
# all `run_loop` and `run_weekly` have in common, which is exactly why the failure handling lives
# here instead of in each of them: two copies of it would be two semantics to keep in step, and the
# one that drifts is the one nobody is reading
# (`jobs/watch/stall_watch.py` and `jobs/watch/qa_keepalive.py` still carry their own older
# variant: they catch and print, with no backoff and no schedule-row write).
#
# A round that raises keeps the state it was given, because the assignment never happened: a slot
# that was never claimed is searched for again, and the next attempt is the same work. The failure
# is handled here rather than in `advance` because only the driver knows what to do next -- back
# off and try again. A failure before the claim -- an unparsable cron, a database that will not
# answer -- would otherwise repeat every round for as long as the loop ran, which is the noise the
# doubling delay exists to bound. A failure after the claim needs no special case: the claim is
# committed, so the next round finds the same slot taken and recovers on its own.
#
# A failed round is not charged to the round budget, because a single forty-minute failure would
# raise every later promise and blunt `schedule_not_progressing`; it still writes a beat, or the
# backoff itself would read as a late loop.
#
# `sleep` is a parameter for the same reason `clock` is: a test drives the loop without waiting.
# Every entry point that reaches it keeps a default, because a schedule row stores its own copy of
# the script and those copies predate these parameters; a required argument would refuse the whole
# cluster's update.
def run_rounds[S](
    db: Database,
    schedule: CronSchedule,
    advance: Callable[[datetime, S], tuple[S, float]],
    *,
    state: S,
    schedule_id: int,
    clock: Callable[[], datetime],
    home: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> NoReturn:
    budget = SCHEDULE_FIRST_ROUND_BUDGET_S
    failures = 0
    write_round_beat(home, schedule_id, now=clock(), sleep_s=0.0, budget_s=budget)
    while True:
        began = clock()
        try:
            state, pause = advance(began, state)
        except Exception as exc:
            failures += 1
            pause = failure_backoff_s(failures, schedule.failure)
            _report_round_failure(db, schedule.failure, schedule_id, failures, pause, exc)
            ended = clock()
            write_round_beat(home, schedule_id, now=ended, sleep_s=pause, budget_s=budget)
            sleep(pause)
            continue
        ended = clock()
        budget = _next_round_budget(budget, (ended - began).total_seconds())
        if failures:
            _log.warning("schedule %s recovered after %d failed rounds", schedule_id, failures)
            _write_last_error(db, schedule_id, None)
            failures = 0
        write_round_beat(home, schedule_id, now=ended, sleep_s=pause, budget_s=budget)
        sleep(pause)


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
    sleep: Callable[[float], None] = time.sleep,
) -> NoReturn:
    # meiva:note
    # One round for a plain cron loop: the claim returns the floor to carry and the pause to take.
    def advance(now: datetime, floor: datetime) -> tuple[datetime, float]:
        tick = loop_once(db, schedule, fire, floor=floor, now=now, schedule_id=schedule_id)
        return tick.floor, tick.sleep

    run_rounds(
        db,
        schedule,
        advance,
        state=started_at,
        schedule_id=schedule_id,
        clock=clock,
        home=home,
        sleep=sleep,
    )
