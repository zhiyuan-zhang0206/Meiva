# meiva:note
# The schedule loop: what it decides from values, and what it does against a real claim table.
#
# `plan_step` and `search_base` are driven with chosen times and asserted on the plan they
# return. `loop_once` runs the same plan with a chosen `now` against the throwaway Postgres:
# what is claimed is read back from `schedule_fire_log`, and the fire callback reports its work
# through its return value, so no test records calls. `run_loop` runs in a child process until
# it claims a slot.

from __future__ import annotations

import multiprocessing
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from schedules.jobs.catchup import Fired
from schedules.jobs.loop import CronSchedule, LoopTick, Trigger, loop_once, run_loop
from schedules.plans.loop_plan import (
    Fire,
    LoopTiming,
    Sleep,
    Upcoming,
    plan_step,
    search_base,
)
from schedules.tests.support.schedule_support import claimed_slots, insert_schedule

_FIVE = datetime(2026, 9, 7, 5, 0, tzinfo=UTC)
_DAILY = CronSchedule((Trigger("0 5 * * *"),), "UTC")


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

    assert tick == LoopTick(120.0, _FIVE, Fired(_FIVE, (_FIVE, None)))
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
    schedule_id: int, started_at: datetime, outcomes: Any, database_gate: ProcessDbGate
) -> None:
    def fire(slot: datetime, trigger: str | None) -> None:
        outcomes.put((slot, trigger))

    run_loop(
        Database.from_settings(gate=database_gate),
        CronSchedule((Trigger("* * * * *", "every-minute"),), "UTC"),
        fire,
        schedule_id=schedule_id,
        started_at=started_at,
    )


def test_run_loop_claims_a_due_slot_in_its_own_process(
    db_conn: psycopg.Connection, database_gate: ProcessDbGate
) -> None:
    assert database_gate.min_read_due(), "spawn copies a fresh admission budget"
    schedule_id = _schedule_row(db_conn)
    context = multiprocessing.get_context("spawn")
    outcomes = context.Queue()
    worker = context.Process(
        target=_run_until_killed,
        args=(schedule_id, datetime.now(UTC) - timedelta(minutes=10), outcomes),
        kwargs={"database_gate": database_gate},
    )

    worker.start()
    try:
        slot, trigger = outcomes.get(timeout=60)
    finally:
        worker.terminate()
        worker.join(timeout=10)

    assert trigger == "every-minute"
    assert claimed_slots(db_conn, schedule_id) == [slot]
