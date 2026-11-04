# meiva:note
# The schedule loop: what it decides from values, and what it does against a real claim table.
#
# `plan_step` and `search_base` are driven with chosen times and asserted on the plan they
# return. `loop_once` runs the same plan with a chosen `now` against the throwaway Postgres:
# what is claimed is read back from `schedule_fire_log`, and the fire callback reports its work
# through its return value, so no test records calls. `run_loop` runs in a child process until
# it claims a slot, and a scripted clock plus a captured `sleep` drive it round by round without
# waiting, which is how a round that raises is followed into the round after it.

from __future__ import annotations

import ast
import json
import logging
import multiprocessing
import signal
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

import psycopg
import pytest

from base.daemon.schedules.heartbeat import Beat, beat_path, read_beat
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.deploy.schedule_timing import SCHEDULE_FIRST_ROUND_BUDGET_S
from schedules.jobs.catchup import Fired
from schedules.jobs.loop import CronSchedule, LoopTick, Trigger, loop_once, run_loop
from schedules.jobs.self_evolution_weekly import run_weekly
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
from schedules.tests.support.schedule_support import claimed_slots, insert_schedule

_FIVE = datetime(2026, 9, 7, 5, 0, tzinfo=UTC)
_DAILY = CronSchedule((Trigger("0 5 * * *"),), "UTC")
_NOON = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
_MINUTE = CronSchedule((Trigger("* * * * *", "every-minute"),), "UTC")


def _work(slot: datetime, trigger: str | None) -> tuple[datetime, str | None]:
    return slot, trigger


def _failing(slot: datetime, trigger: str | None) -> None:
    raise RuntimeError(f"fire failed for {slot.isoformat()} {trigger}")


def _schedule_row(conn: psycopg.Connection) -> int:
    return insert_schedule(conn, name="loop-test", created_at=_FIVE - timedelta(days=1))


_PLAN_SLOT = datetime(2026, 9, 7, 5, 0, tzinfo=UTC)
_PLAN_DAILY = LoopTiming()


def _upcoming(*slots: tuple[datetime, str | None]) -> tuple[Upcoming, ...]:
    return tuple(Upcoming(slot, trigger) for slot, trigger in slots)


def test_search_starts_two_minutes_before_now_unless_the_floor_is_later() -> None:
    now = datetime(2026, 9, 7, 5, 0, 30, tzinfo=UTC)
    assert search_base(now - timedelta(hours=1), now, _PLAN_DAILY) == now - timedelta(minutes=2)
    floor = now - timedelta(seconds=30)
    assert search_base(floor, now, _PLAN_DAILY) == floor


def test_a_slot_that_passed_before_the_process_started_is_never_searched_for() -> None:
    started = _PLAN_SLOT + timedelta(seconds=30)
    now = _PLAN_SLOT + timedelta(minutes=1)
    assert search_base(started, now, _PLAN_DAILY) > _PLAN_SLOT


def test_a_wake_a_fraction_past_the_slot_still_claims_it() -> None:
    now = _PLAN_SLOT + timedelta(milliseconds=300)
    assert plan_step(_upcoming((_PLAN_SLOT, None)), now, _PLAN_DAILY) == Fire(
        _PLAN_SLOT, None, 120.0
    )


def test_a_slot_not_yet_due_sleeps_exactly_until_it() -> None:
    now = _PLAN_SLOT - timedelta(seconds=45)
    assert plan_step(_upcoming((_PLAN_SLOT, None)), now, _PLAN_DAILY) == Sleep(45.0)


def test_a_distant_slot_sleeps_at_most_an_hour() -> None:
    now = _PLAN_SLOT - timedelta(hours=7)
    assert plan_step(_upcoming((_PLAN_SLOT, None)), now, _PLAN_DAILY) == Sleep(3600.0)


def test_a_loop_that_fires_early_claims_inside_its_window_and_sleeps_outside_it() -> None:
    timing = LoopTiming(fire_ahead=60.0)
    assert plan_step(
        _upcoming((_PLAN_SLOT, None)), _PLAN_SLOT - timedelta(seconds=61), timing
    ) == Sleep(61.0)
    assert plan_step(
        _upcoming((_PLAN_SLOT, None)), _PLAN_SLOT - timedelta(seconds=60), timing
    ) == Fire(_PLAN_SLOT, None, 120.0)


def test_the_earliest_due_trigger_is_claimed_and_carries_its_payload() -> None:
    later = _PLAN_SLOT + timedelta(days=3)
    now = _PLAN_SLOT + timedelta(seconds=1)
    upcoming = _upcoming((later, "thursday"), (_PLAN_SLOT, "monday"))
    assert plan_step(upcoming, now, LoopTiming(fire_ahead=90.0)) == Fire(
        _PLAN_SLOT, "monday", 120.0
    )


def test_equally_early_triggers_resolve_in_declared_order() -> None:
    upcoming = _upcoming((_PLAN_SLOT, "first"), (_PLAN_SLOT, "second"))
    assert plan_step(upcoming, _PLAN_SLOT, _PLAN_DAILY) == Fire(_PLAN_SLOT, "first", 120.0)


def test_with_nothing_due_the_nearest_trigger_sets_the_sleep() -> None:
    now = _PLAN_SLOT - timedelta(minutes=10)
    upcoming = _upcoming((_PLAN_SLOT + timedelta(days=1), "far"), (_PLAN_SLOT, "near"))
    assert plan_step(upcoming, now, _PLAN_DAILY) == Sleep(600.0)


def test_a_loop_that_rests_less_reports_its_own_rest() -> None:
    assert plan_step(_upcoming((_PLAN_SLOT, None)), _PLAN_SLOT, LoopTiming(rest=60.0)) == Fire(
        _PLAN_SLOT, None, 60.0
    )


def test_a_loop_without_triggers_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one trigger"):
        plan_step((), _PLAN_SLOT, _PLAN_DAILY)


def test_a_naive_clock_value_is_refused() -> None:
    naive = _PLAN_SLOT.replace(tzinfo=None)
    with pytest.raises(ValueError, match="now must carry tzinfo"):
        plan_step(_upcoming((_PLAN_SLOT, None)), naive, _PLAN_DAILY)
    with pytest.raises(ValueError, match="floor must carry tzinfo"):
        search_base(naive, _PLAN_SLOT, _PLAN_DAILY)


def test_a_due_slot_is_claimed_and_the_callbacks_result_comes_back(
    db_conn: psycopg.Connection, database: Database
) -> None:
    schedule_id = _schedule_row(db_conn)
    now = _FIVE + timedelta(seconds=30)

    tick = loop_once(
        database,
        _DAILY,
        _work,
        floor=_FIVE - timedelta(hours=1),
        now=now,
        schedule_id=schedule_id,
    )

    # meiva:note
    # The fields, not the whole tick: a `Fired` payload's type argument cannot be solved from a class
    # another module defines under the impacted-closure typecheck (`LoopTick` reads as `LoopTick[Unknown]`
    # there), and `Fired` is invariant, so the whole-object form is unprovable without a cast.
    assert (tick.sleep, tick.floor, tick.fired) == (120.0, _FIVE, Fired(_FIVE, (_FIVE, None)))
    assert claimed_slots(db_conn, schedule_id) == [_FIVE]


def test_the_next_iteration_after_a_claim_sleeps_toward_the_next_day(
    db_conn: psycopg.Connection, database: Database
) -> None:
    schedule_id = _schedule_row(db_conn)
    first = loop_once(
        database,
        _DAILY,
        _work,
        floor=_FIVE - timedelta(hours=1),
        now=_FIVE,
        schedule_id=schedule_id,
    )

    second = loop_once(
        database,
        _DAILY,
        _work,
        floor=first.floor,
        now=_FIVE + timedelta(seconds=120),
        schedule_id=schedule_id,
    )

    assert second == LoopTick(3600.0, _FIVE, None)
    assert claimed_slots(db_conn, schedule_id) == [_FIVE]


def test_a_second_runner_for_the_same_slot_loses_the_claim(
    db_conn: psycopg.Connection, database: Database
) -> None:
    schedule_id = _schedule_row(db_conn)
    floor = _FIVE - timedelta(hours=1)
    now = _FIVE + timedelta(seconds=10)
    won = loop_once(database, _DAILY, _work, floor=floor, now=now, schedule_id=schedule_id)

    lost = loop_once(database, _DAILY, _work, floor=floor, now=now, schedule_id=schedule_id)

    assert won.fired is not None
    assert lost == LoopTick(120.0, _FIVE, None)
    assert claimed_slots(db_conn, schedule_id) == [_FIVE]


def test_nothing_due_returns_the_sleep_and_claims_nothing(
    db_conn: psycopg.Connection, database: Database
) -> None:
    schedule_id = _schedule_row(db_conn)
    floor = _FIVE - timedelta(hours=6)

    near = loop_once(
        database,
        _DAILY,
        _work,
        floor=floor,
        now=_FIVE - timedelta(seconds=45),
        schedule_id=schedule_id,
    )
    far = loop_once(
        database,
        _DAILY,
        _work,
        floor=floor,
        now=_FIVE - timedelta(hours=3),
        schedule_id=schedule_id,
    )

    assert near == LoopTick(45.0, floor, None)
    assert far == LoopTick(3600.0, floor, None)
    assert claimed_slots(db_conn, schedule_id) == []


def test_a_loop_that_fires_early_claims_its_slot_once(
    db_conn: psycopg.Connection, database: Database
) -> None:
    schedule = CronSchedule((Trigger("0 5 * * *"),), "UTC", LoopTiming(fire_ahead=60.0))
    schedule_id = _schedule_row(db_conn)
    early = loop_once(
        database,
        schedule,
        _work,
        floor=_FIVE - timedelta(hours=1),
        now=_FIVE - timedelta(seconds=30),
        schedule_id=schedule_id,
    )

    after_rest = loop_once(
        database,
        schedule,
        _work,
        floor=early.floor,
        now=_FIVE + timedelta(seconds=90),
        schedule_id=schedule_id,
    )

    assert early.fired == Fired(_FIVE, (_FIVE, None))
    assert after_rest.fired is None
    assert after_rest.sleep == 3600.0
    assert claimed_slots(db_conn, schedule_id) == [_FIVE]


def test_a_restart_after_the_slot_does_not_replay_it(
    db_conn: psycopg.Connection, database: Database
) -> None:
    schedule_id = _schedule_row(db_conn)
    started = _FIVE + timedelta(seconds=30)

    tick = loop_once(
        database,
        _DAILY,
        _work,
        floor=started,
        now=_FIVE + timedelta(minutes=1),
        schedule_id=schedule_id,
    )

    assert tick == LoopTick(3600.0, started, None)
    assert claimed_slots(db_conn, schedule_id) == []


def test_the_due_trigger_hands_its_payload_to_the_callback(
    db_conn: psycopg.Connection, database: Database
) -> None:
    weekly = CronSchedule(
        (Trigger("0 0 * * 2", "main"), Trigger("0 0 * * 5", "follow-up")),
        "UTC",
        LoopTiming(fire_ahead=90.0),
    )
    friday = datetime(2026, 9, 11, tzinfo=UTC)
    schedule_id = _schedule_row(db_conn)

    tick = loop_once(
        database,
        weekly,
        _work,
        floor=friday - timedelta(days=2),
        now=friday - timedelta(seconds=30),
        schedule_id=schedule_id,
    )

    assert tick.fired == Fired(friday, (friday, "follow-up"))
    assert claimed_slots(db_conn, schedule_id) == [friday]


def test_a_failing_callback_leaves_the_slot_claimed(
    db_conn: psycopg.Connection, database: Database
) -> None:
    schedule_id = _schedule_row(db_conn)
    floor = _FIVE - timedelta(hours=1)

    with pytest.raises(RuntimeError, match="fire failed"):
        loop_once(database, _DAILY, _failing, floor=floor, now=_FIVE, schedule_id=schedule_id)

    retry = loop_once(database, _DAILY, _work, floor=floor, now=_FIVE, schedule_id=schedule_id)
    assert retry.fired is None
    assert claimed_slots(db_conn, schedule_id) == [_FIVE]


def _run_until_killed(
    schedule_id: int,
    started_at: datetime,
    outcomes: Any,
    database_gate: ProcessDbGate,
    home: Path,
) -> None:
    def fire(slot: datetime, trigger: str | None) -> None:
        outcomes.put((slot, trigger))

    run_loop(
        Database.from_settings(gate=database_gate),
        CronSchedule((Trigger("* * * * *", "every-minute"),), "UTC"),
        fire,
        schedule_id=schedule_id,
        started_at=started_at,
        clock=lambda: datetime.now(UTC),
        home=home,
    )


def test_run_loop_claims_a_due_slot_in_its_own_process(
    db_conn: psycopg.Connection, database_gate: ProcessDbGate, tmp_path: Path
) -> None:
    assert database_gate.min_read_due(), "spawn copies a fresh admission budget"
    schedule_id = _schedule_row(db_conn)
    context = multiprocessing.get_context("spawn")
    outcomes = context.Queue()
    worker = context.Process(
        target=_run_until_killed,
        args=(schedule_id, datetime.now(UTC) - timedelta(minutes=10), outcomes),
        kwargs={"database_gate": database_gate, "home": tmp_path},
    )

    worker.start()
    try:
        slot, trigger = outcomes.get(timeout=60)
    finally:
        worker.terminate()
        worker.join(timeout=10)

    assert trigger == "every-minute"
    assert claimed_slots(db_conn, schedule_id) == [slot]
    # meiva:note
    # The loop now survives a round that raises, so the other half has to stay true: a supervisor
    # stopping it still stops it. A signal is not an `Exception`; catching one here would leave a
    # schedule running after its manager asked it to go.
    assert worker.exitcode == -signal.SIGTERM, "the loop must die on SIGTERM, not swallow it"


# meiva:note
# The loop writes the heartbeat the manager reads, without any schedule script asking it to: a
# process that dies stops refreshing the promise, and the manager's round clock is the only fast
# one that exists. The child process proves the file lands; the budget test states the promise the
# file carries, which is what makes a missed round visible instead of merely quiet.
def test_run_loop_writes_the_round_heartbeat_its_manager_reads(
    db_conn: psycopg.Connection, database_gate: ProcessDbGate, tmp_path: Path
) -> None:
    schedule_id = _schedule_row(db_conn)
    context = multiprocessing.get_context("spawn")
    outcomes = context.Queue()
    worker = context.Process(
        target=_run_until_killed,
        args=(schedule_id, datetime.now(UTC) - timedelta(minutes=10), outcomes),
        kwargs={"database_gate": database_gate, "home": tmp_path},
    )

    worker.start()
    try:
        outcomes.get(timeout=60)
    finally:
        worker.terminate()
        worker.join(timeout=10)

    beat = read_beat(tmp_path, schedule_id)
    assert beat is not None, "the loop must leave the heartbeat its manager reads"
    assert beat.schedule_id == schedule_id
    assert beat.alive_by - beat.written_at >= timedelta(seconds=SCHEDULE_FIRST_ROUND_BUDGET_S)
    payload = json.loads(beat_path(tmp_path, schedule_id).read_text())
    assert payload["round_budget_s"] >= SCHEDULE_FIRST_ROUND_BUDGET_S


# meiva:note
# The failure half of the loop. Before it, a round that raised ended the process of whichever
# schedule it belonged to -- the thirteen templates on `run_loop` and the weekly one on `run_weekly`
# alike -- and each needed a person to restart it. The tests below drive a loop through a scripted
# clock and a captured `sleep`, so a failure is followed into the round after it and the pause it
# asked for is the assertion -- nothing here waits.
class _StopError(Exception):
    # meiva:note
    # Ends a loop that would otherwise never return: the test's own sleep raises it.
    pass


# meiva:note
# The `observe` seam for a test that only wants the pauses: it reads nothing between rounds.
def _nothing() -> None:
    pass


# meiva:note
# A clock that answers each round's two reads with that round's start. The driver reads the clock
# once for the opening beat and twice per round (start and end), so each instant is used twice with
# the first used once more at the front. Past the last round it repeats that round's instant: a
# test that stops at a sleep never reads further.
def _clock_over(*rounds: datetime) -> Callable[[], datetime]:
    instants = [rounds[0]] + [instant for start in rounds for instant in (start, start)]
    remaining = iter(instants)
    return lambda: next(remaining, rounds[-1])


def _last_error(conn: psycopg.Connection, schedule_id: int) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT last_error FROM schedules WHERE id = %s", (schedule_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _drive(
    run: Callable[[Callable[[float], None]], NoReturn],
    *,
    rounds: int,
    observe: Callable[[], None],
) -> list[float]:
    # meiva:note
    # Run a loop for `rounds` rounds and return the pause it asked for after each. `run` is handed
    # the sleep seam and calls the loop -- `run_loop` or `run_weekly`, both of which end in
    # `run_rounds` -- so one driver covers both. `observe` is how a test reads the world between
    # rounds (the schedule row, the heartbeat file), and the raise is the only way out of a
    # `NoReturn`.
    pauses: list[float] = []

    def sleep(seconds: float) -> None:
        pauses.append(seconds)
        observe()
        if len(pauses) >= rounds:
            raise _StopError

    with pytest.raises(_StopError):
        run(sleep)
    return pauses


def test_the_backoff_doubles_from_the_first_delay_and_stops_at_the_cap() -> None:
    policy = FailurePolicy()
    assert [failure_backoff_s(failures, policy) for failures in range(1, 7)] == [
        60.0,
        120.0,
        240.0,
        300.0,
        300.0,
        300.0,
    ]


def test_a_policy_that_could_sleep_too_little_or_never_report_is_refused() -> None:
    with pytest.raises(ValueError, match="first_delay_s must be positive"):
        FailurePolicy(first_delay_s=0.0)
    with pytest.raises(ValueError, match="must not be below first_delay_s"):
        FailurePolicy(first_delay_s=120.0, max_delay_s=60.0)
    with pytest.raises(ValueError, match="report_after must be at least 1"):
        FailurePolicy(report_after=0)
    with pytest.raises(ValueError, match="failures must be at least 1"):
        failure_backoff_s(0, FailurePolicy())


# meiva:note
# The one behaviour this whole change exists for: a callback that raised used to end the loop. The
# round after it finds the slot claimed, advances the floor and carries on -- one extra round, then
# normal pacing -- and the pause in between is the policy's first delay, not the loop's rest.
def test_a_round_that_raises_does_not_end_the_loop(
    db_conn: psycopg.Connection, database: Database, tmp_path: Path
) -> None:
    schedule_id = _schedule_row(db_conn)
    started_at = _NOON + timedelta(seconds=30)
    calls: list[datetime] = []

    def fire(slot: datetime, trigger: str | None) -> None:
        calls.append(slot)
        raise RuntimeError("fire failed")

    pauses = _drive(
        lambda sleep: run_loop(
            database,
            _MINUTE,
            fire,
            schedule_id=schedule_id,
            started_at=started_at,
            clock=_clock_over(
                _NOON + timedelta(minutes=1), _NOON + timedelta(minutes=1, seconds=10)
            ),
            home=tmp_path,
            sleep=sleep,
        ),
        rounds=2,
        observe=_nothing,
    )

    assert pauses == [60.0, 120.0]
    assert calls == [_NOON + timedelta(minutes=1)], "the claimed slot is not replayed"
    assert claimed_slots(db_conn, schedule_id) == [_NOON + timedelta(minutes=1)]


# meiva:note
# A failure that happens before the claim -- here an unparsable cron, raised in `next_fire` -- is
# the one that would repeat every round for as long as the loop ran, so it is the one the doubling
# delay bounds. Five rounds is past the cap: the last two pauses are equal, and the escalation
# (one error with a traceback, never one per round) landed on the third.
def test_a_failure_before_the_claim_backs_off_to_the_cap_and_reports_once(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    schedule_id = _schedule_row(db_conn)
    broken = CronSchedule((Trigger("not a cron expression"),), "UTC")

    pauses = _drive(
        lambda sleep: run_loop(
            database,
            broken,
            _work,
            schedule_id=schedule_id,
            started_at=_NOON,
            clock=_clock_over(_NOON),
            home=tmp_path,
            sleep=sleep,
        ),
        rounds=5,
        observe=_nothing,
    )

    assert pauses == [60.0, 120.0, 240.0, 300.0, 300.0]
    escalations = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(escalations) == 1
    assert "3 rounds in a row" in escalations[0].getMessage()
    assert escalations[0].exc_info is not None, "the escalation must carry the traceback"
    assert claimed_slots(db_conn, schedule_id) == [], "nothing was claimed, so nothing is lost"


# meiva:note
# The count is per episode, not per process: after a round succeeds the next failure waits the
# first delay again (60 s, not 120 s), and says so in the log. Three rounds -- fail, claim-lost
# recovery, fail -- are enough to see both.
def test_a_recovered_loop_starts_its_backoff_over(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    schedule_id = _schedule_row(db_conn)
    failing = {_NOON + timedelta(minutes=1), _NOON + timedelta(minutes=2)}

    def fire(slot: datetime, trigger: str | None) -> None:
        if slot in failing:
            raise RuntimeError(f"fire failed for {slot.isoformat()} {trigger}")

    pauses = _drive(
        lambda sleep: run_loop(
            database,
            _MINUTE,
            fire,
            schedule_id=schedule_id,
            started_at=_NOON + timedelta(seconds=30),
            clock=_clock_over(
                _NOON + timedelta(minutes=1),
                _NOON + timedelta(minutes=1, seconds=30),
                _NOON + timedelta(minutes=2),
            ),
            home=tmp_path,
            sleep=sleep,
        ),
        rounds=3,
        observe=_nothing,
    )

    assert pauses == [60.0, 120.0, 60.0], "a recovered loop restarts at the first delay"
    recovered = [r.getMessage() for r in caplog.records if "recovered after" in r.getMessage()]
    assert recovered == [f"schedule {schedule_id} recovered after 1 failed rounds"]


# meiva:note
# The loop must not catch what is not an error to recover from. `KeyboardInterrupt` and the signal
# exits are how a supervisor stops a resident process, so they travel straight out -- the `sleep`
# seam would be reached only if the loop had swallowed the interrupt.
def test_a_control_flow_interrupt_ends_the_loop(
    db_conn: psycopg.Connection, database: Database, tmp_path: Path
) -> None:
    schedule_id = _schedule_row(db_conn)

    def fire(slot: datetime, trigger: str | None) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_loop(
            database,
            _MINUTE,
            fire,
            schedule_id=schedule_id,
            started_at=_NOON + timedelta(seconds=30),
            clock=_clock_over(_NOON + timedelta(minutes=1)),
            home=tmp_path,
            sleep=lambda seconds: pytest.fail("the loop slept instead of honouring the interrupt"),
        )


# meiva:note
# A loop that survives a failure is quieter than one that died of it, and this is what keeps that
# from being a net loss: the escalation writes the column `meiva schedules ls` shows, and the round
# that succeeds clears it. Read from inside the loop's own `sleep`, against a second connection, so
# the assertion is about what a reader outside the process would have seen.
def test_an_escalated_failure_lands_in_the_schedule_row_and_recovery_clears_it(
    db_conn: psycopg.Connection, database: Database, tmp_path: Path
) -> None:
    schedule_id = _schedule_row(db_conn)
    seen: list[str | None] = []

    def fire(slot: datetime, trigger: str | None) -> None:
        raise RuntimeError("boom-7392")

    pauses = _drive(
        lambda sleep: run_loop(
            database,
            CronSchedule(
                (Trigger("* * * * *", "every-minute"),),
                "UTC",
                failure=FailurePolicy(report_after=1),
            ),
            fire,
            schedule_id=schedule_id,
            started_at=_NOON + timedelta(seconds=30),
            clock=_clock_over(
                _NOON + timedelta(minutes=1), _NOON + timedelta(minutes=1, seconds=10)
            ),
            home=tmp_path,
            sleep=sleep,
        ),
        rounds=2,
        observe=lambda: seen.append(_last_error(db_conn, schedule_id)),
    )

    assert pauses == [60.0, 120.0]
    assert seen[0] is not None and "boom-7392" in seen[0]
    assert "RuntimeError" in seen[0] and "next attempt in 60s" in seen[0]
    assert seen[1] is None, "recovery clears the error a person would otherwise keep reading"


# meiva:note
# The weekly loop is the second entry point into `run_rounds`, and it was the one loop left with no
# protection at all when the daily one was fixed -- its own `while True` over `weekly_once`, with no
# except and not even a heartbeat. The same proof, with a different round: the callback raises, and
# the loop still takes the round after it.
_TUESDAY = datetime(2026, 9, 8, tzinfo=UTC)


def _beat(home: Path, schedule_id: int) -> Beat:
    beat = read_beat(home, schedule_id)
    assert beat is not None, "the loop must leave the heartbeat its manager reads"
    return beat


def test_a_round_that_raises_does_not_end_the_weekly(
    db_conn: psycopg.Connection, database: Database, tmp_path: Path
) -> None:
    schedule_id = _schedule_row(db_conn)
    weekly = CronSchedule((Trigger("0 0 * * 2", "main"),), "UTC")
    calls: list[datetime] = []

    def fire(slot: datetime, trigger: str | None) -> bool:
        calls.append(slot)
        raise RuntimeError("weekly fire failed")

    pauses = _drive(
        lambda sleep: run_weekly(
            database,
            weekly,
            fire,
            schedule_id=schedule_id,
            started_at=_TUESDAY - timedelta(hours=1),
            clock=_clock_over(_TUESDAY, _TUESDAY + timedelta(seconds=10)),
            home=tmp_path,
            sleep=sleep,
        ),
        rounds=2,
        observe=_nothing,
    )

    assert pauses == [60.0, 120.0], "back off, then the loop's own rest"
    assert calls == [_TUESDAY], "the claimed slot is not replayed"
    assert claimed_slots(db_conn, schedule_id) == [_TUESDAY]


# meiva:note
# "It did not die" is half of it: a loop that kept running but stopped moving its beat would trade
# a dead process for a blind one, which is the failure this whole change is trying to stop being
# quiet. Each beat is read between rounds, from the file the manager reads, and has to promise a
# later instant than the one before it and cover the pause the loop is about to take.
def test_a_failing_weekly_round_still_moves_the_round_beat(
    db_conn: psycopg.Connection, database: Database, tmp_path: Path
) -> None:
    schedule_id = _schedule_row(db_conn)
    broken = CronSchedule((Trigger("not a cron expression", "main"),), "UTC")
    beats: list[Beat] = []
    moments = (_TUESDAY, _TUESDAY + timedelta(minutes=5), _TUESDAY + timedelta(minutes=20))

    def fire(slot: datetime, trigger: str | None) -> bool:
        raise AssertionError("an unparsable cron never reaches the callback")

    pauses = _drive(
        lambda sleep: run_weekly(
            database,
            broken,
            fire,
            schedule_id=schedule_id,
            started_at=_TUESDAY - timedelta(hours=1),
            clock=_clock_over(*moments),
            home=tmp_path,
            sleep=sleep,
        ),
        rounds=3,
        observe=lambda: beats.append(_beat(tmp_path, schedule_id)),
    )

    assert pauses == [60.0, 120.0, 240.0]
    assert [beat.written_at for beat in beats] == list(moments)
    assert [beat.alive_by for beat in beats] == sorted(beat.alive_by for beat in beats)
    assert [beat.alive_by - beat.written_at for beat in beats] == [
        timedelta(seconds=SCHEDULE_FIRST_ROUND_BUDGET_S + pause) for pause in pauses
    ], "each beat has to cover the pause the loop is about to take"


# meiva:note
# Every template hands its loop its home: `run_loop` for the cron loops and `run_weekly` for the
# weekly one, both of which reach `run_rounds` and its beat. The default exists only for a stored
# script that predates the parameter, so a template that forgot it would run without a heartbeat
# and nothing would say so -- the exact silence this loop change exists to end. The scan reads the
# repository's own templates: no database, no process.
def test_every_schedule_template_passes_its_home_to_the_loop() -> None:
    root = Path(__file__).resolve().parents[2]
    missing: list[str] = []
    for path in sorted(root.glob("schedules/**/*.py")):
        if "tests" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", "") not in {"run_loop", "run_weekly"}:
                continue
            if not any(keyword.arg == "home" for keyword in node.keywords):
                missing.append(f"{path.relative_to(root)}:{node.lineno}")
    assert missing == [], f"templates that would run without a heartbeat: {missing}"
