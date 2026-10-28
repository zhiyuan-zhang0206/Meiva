# meiva:note
# The agent watch's two halves: what the rule decides from values (`plan_round`,
# `judge_schedule`) and what one round reads from the database (`observe`).
#
# The sends are not tested here. What matters is the ladder -- one wake at the first silent round,
# a report once the silence passes its interval, an escalation only after that report, a widening
# interval between repeats -- that a declared pause is not silence, and that a reading the round
# could not take is carried as blind instead of passing for healthy.

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from schedules.jobs.agent_watch import observe, read_state, round_line, write_state
from schedules.plans.agent_watch_rule import (
    ESCALATED_REPEAT_SECONDS,
    FIRST_REPEAT_SECONDS,
    REPORT_REPEAT_SECONDS,
    Observation,
    ScheduleVerdict,
    WatchedAgent,
    WatchState,
    judge_schedule,
    plan_round,
    repeat_seconds,
    silent_reasons,
)
from schedules.tests.support.schedule_support import insert_schedule

_DUTY = WatchedAgent(
    agent_id=4242,
    duty="test duty",
    patrol_minutes=60.0,
    keeps_sessions=True,
    schedules=("watch-test",),
)
_LEAD = 405
_STAMP = "2026-10-27T08:00:00+00:00"
_ALIVE = Observation("idling", 3, 10.0)
_SILENT = Observation("idling", 0, 200.0)


def _plan(observation: Observation, state: WatchState, *, now: float):
    return plan_round(_DUTY, observation, state, now=now, stamp=_STAMP, report_agent=_LEAD)


def test_a_duty_with_sessions_and_a_recent_turn_is_alive() -> None:
    plan = _plan(_ALIVE, WatchState(silent_since=1000.0), now=2000.0)
    assert plan.reasons == ()
    assert plan.actions == ()
    assert plan.state == WatchState()
    assert "recovered after 17m" in plan.log[0]


def test_zero_sessions_wakes_at_the_first_silent_round() -> None:
    plan = _plan(_SILENT, WatchState(), now=5000.0)
    assert plan.reasons == ("sessions=0", "active=200m")
    assert [action.kind for action in plan.actions] == ["wake"]
    assert plan.actions[0].target == _DUTY.agent_id
    assert plan.state.silent_since == 5000.0
    assert "A(sessions)=0" in plan.actions[0].text


def test_a_wake_holds_its_cooldown_and_the_ladder_climbs() -> None:
    first = _plan(_SILENT, WatchState(), now=5000.0)
    assert [action.kind for action in first.actions] == ["wake"]

    during = _plan(_SILENT, first.state, now=5000.0 + FIRST_REPEAT_SECONDS - 1)
    assert during.actions == ()

    report = _plan(_SILENT, during.state, now=5000.0 + 181 * 60)
    assert [action.kind for action in report.actions] == ["wake", "report"]
    assert report.actions[1].target == _LEAD

    escalate = _plan(_SILENT, report.state, now=5000.0 + 241 * 60)
    assert [action.kind for action in escalate.actions] == ["escalate"]
    assert escalate.actions[0].target == _LEAD
    assert "[escalate-to-user]" in escalate.actions[0].text


def test_a_declared_pause_is_not_silence_but_a_dead_fleet_still_is() -> None:
    paused = Observation("idling", 3, 400.0, 120.0)
    assert silent_reasons(_DUTY, paused) == ()
    assert _plan(paused, WatchState(), now=2000.0).actions == ()

    paused_without_sessions = Observation("idling", 0, 400.0, 120.0)
    assert silent_reasons(_DUTY, paused_without_sessions) == ("sessions=0",)
    assert [
        action.kind for action in _plan(paused_without_sessions, WatchState(), now=2000.0).actions
    ] == ["wake"]


@pytest.mark.parametrize("patrol", [60.0, 120.0, 240.0])
def test_the_escalation_never_overtakes_the_report(patrol: float) -> None:
    duty = replace(_DUTY, patrol_minutes=patrol)
    report_at = 3.0 * patrol
    escalate_at = max(240.0, report_at)
    state = WatchState(silent_since=5000.0)

    # meiva:note
    # One minute before the escalation threshold: the lead may get the report, never the escalation.
    before = plan_round(
        duty, _SILENT, state, now=5000.0 + (escalate_at - 1) * 60, stamp=_STAMP, report_agent=_LEAD
    )
    kinds = [action.kind for action in before.actions]
    assert "escalate" not in kinds
    if escalate_at - 1 >= report_at:
        assert "report" in kinds

    # meiva:note
    # At the threshold the report has already gone out, in this round or an earlier one.
    state = WatchState(last_report_at=5000.0 + report_at * 60, silent_since=5000.0)
    at = plan_round(
        duty, _SILENT, state, now=5000.0 + escalate_at * 60, stamp=_STAMP, report_agent=_LEAD
    )
    assert "escalate" in [action.kind for action in at.actions]


def test_the_interval_widens_after_the_report_and_the_escalation() -> None:
    assert repeat_seconds(WatchState()) == FIRST_REPEAT_SECONDS
    assert repeat_seconds(WatchState(last_report_at=1.0)) == REPORT_REPEAT_SECONDS
    assert (
        repeat_seconds(WatchState(last_report_at=1.0, last_escalate_at=2.0))
        == ESCALATED_REPEAT_SECONDS
    )

    # meiva:note
    # A stuck duty: three hours after the report the next message waits four hours, not one.
    last_message = 5000.0 + 180 * 60
    state = WatchState(silent_since=5000.0, last_wake_at=last_message, last_report_at=last_message)
    quiet = plan_round(
        _DUTY, _SILENT, state, now=last_message + 601, stamp=_STAMP, report_agent=_LEAD
    )
    assert quiet.actions == ()


def test_a_terminated_duty_is_finished_not_silent() -> None:
    plan = _plan(Observation("terminated", 0, None), WatchState(silent_since=1000.0), now=2000.0)
    assert plan.reasons == ()
    assert plan.actions == ()
    assert plan.state == WatchState()
    assert "status=terminated" in plan.log[0]


def test_a_blind_reading_is_carried_not_counted_as_healthy() -> None:
    blind = Observation("idling", 3, 10.0, blind=("watch-test: 2 fires, no cadence",))
    plan = _plan(blind, WatchState(), now=2000.0)
    assert plan.reasons == ()
    assert "blind" in plan.log[0]

    silent = _plan(
        Observation("idling", 0, 200.0, blind=("active: NULL last_active_at",)),
        WatchState(),
        now=2000.0,
    )
    assert "blind: active: NULL" in silent.actions[0].text


def test_a_duty_that_keeps_no_sessions_is_not_woken_for_having_none() -> None:
    duty = WatchedAgent(agent_id=7, duty="d", patrol_minutes=60.0, keeps_sessions=False)
    assert silent_reasons(duty, Observation("idling", 0, 10.0)) == ()


def _fires(
    now: datetime, *, count: int, gap_minutes: int, last_minutes_ago: int
) -> tuple[datetime, ...]:
    newest = now - timedelta(minutes=last_minutes_ago)
    return tuple(newest - timedelta(minutes=gap_minutes * step) for step in range(count))


def test_a_schedule_needs_its_own_cadence_before_it_can_be_late() -> None:
    now = datetime(2026, 10, 27, 8, 0, tzinfo=UTC)
    few = _fires(now, count=3, gap_minutes=60, last_minutes_ago=999)
    assert judge_schedule("s", few, now=now) == ScheduleVerdict(blind="s: 3 fires, no cadence")

    on_time = _fires(now, count=6, gap_minutes=60, last_minutes_ago=30)
    assert judge_schedule("s", on_time, now=now) == ScheduleVerdict()

    late = _fires(now, count=6, gap_minutes=60, last_minutes_ago=300)
    verdict = judge_schedule("s", late, now=now)
    assert verdict.blind is None
    assert verdict.late == "s: last fire 300m ago, cadence 60m"


def _insert_agent(conn: psycopg.Connection, *, status: str, active_minutes: float | None) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        agent_id = int(row[0])
        cur.execute(
            "INSERT INTO agents_meta (id, status, last_active_at) VALUES (%s, %s, %s)",
            (
                agent_id,
                status,
                None
                if active_minutes is None
                else datetime.now(UTC) - timedelta(minutes=active_minutes),
            ),
        )
    conn.commit()
    return agent_id


def _insert_sessions(conn: psycopg.Connection, agent_id: int, *, live: int, expired: int) -> None:
    with conn.cursor() as cur:
        for step in range(live):
            cur.execute(
                "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) "
                "VALUES (%s, %s, now() + interval '1 hour')",
                (agent_id, step + 1),
            )
        for step in range(expired):
            cur.execute(
                "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) "
                "VALUES (%s, %s, now() - interval '1 hour')",
                (agent_id, live + step + 1),
            )
    conn.commit()


def test_observe_counts_only_unexpired_sessions_and_reads_the_status(
    db_conn: psycopg.Connection,
) -> None:
    agent_id = _insert_agent(db_conn, status="idling", active_minutes=200.0)
    _insert_sessions(db_conn, agent_id, live=2, expired=3)
    entry = WatchedAgent(agent_id, "d", 60.0, True)

    observation = observe(db_conn, entry, now=datetime.now(UTC))

    assert observation.status == "idling"
    assert observation.sessions == 2
    assert observation.active_minutes == pytest.approx(200.0, abs=2.0)


def test_observe_marks_a_disabled_schedule_as_neither_late_nor_blind(
    db_conn: psycopg.Connection,
) -> None:
    agent_id = _insert_agent(db_conn, status="idling", active_minutes=10.0)
    insert_schedule(db_conn, name="watch-test", created_at=datetime.now(UTC) - timedelta(days=2))
    with db_conn.cursor() as cur:
        cur.execute("UPDATE schedules SET enabled = false WHERE name = 'watch-test'")
    db_conn.commit()
    entry = WatchedAgent(agent_id, "d", 60.0, True, ("watch-test",))

    observation = observe(db_conn, entry, now=datetime.now(UTC))

    assert observation.late_schedules == ()
    assert observation.blind == ()


def test_observe_reports_a_schedule_that_stopped_firing_on_its_cadence(
    db_conn: psycopg.Connection,
) -> None:
    agent_id = _insert_agent(db_conn, status="idling", active_minutes=10.0)
    schedule_id = insert_schedule(
        db_conn, name="watch-test", created_at=datetime.now(UTC) - timedelta(days=2)
    )
    now = datetime.now(UTC)
    with db_conn.cursor() as cur:
        for fire in _fires(now, count=6, gap_minutes=10, last_minutes_ago=120):
            cur.execute(
                "INSERT INTO schedule_fire_log (schedule_id, slot_fire_at) VALUES (%s, %s)",
                (schedule_id, fire),
            )
    db_conn.commit()
    entry = WatchedAgent(agent_id, "d", 60.0, True, ("watch-test",))

    observation = observe(db_conn, entry, now=now)

    assert observation.blind == ()
    assert observation.late_schedules == ("watch-test: last fire 120m ago, cadence 10m",)


def test_the_round_line_names_what_was_silent_and_what_was_blind() -> None:
    plans = [(_DUTY, _plan(_SILENT, WatchState(), now=5000.0))]
    line = round_line(
        [_DUTY], plans, {_DUTY.agent_id: Observation("idling", 0, 200.0, blind=("x",))}
    )
    assert "watched=1" in line
    assert "silent=1" in line
    assert "blind=1" in line
    assert f"silent_ids={_DUTY.agent_id}" in line
    assert "emit_failures" not in line


# meiva:note
# A round whose event could not be emitted is a round nobody can see, and it used to read exactly
# like a round with nothing to report. The count rides on the line the round prints, so the failure
# is visible in the same place as the round's other readings.
def test_the_round_line_carries_the_count_of_emit_failures() -> None:
    plans = [(_DUTY, _plan(_ALIVE, WatchState(), now=5000.0))]
    line = round_line([_DUTY], plans, {_DUTY.agent_id: _ALIVE}, emit_failures=3)
    assert "emit_failures=3" in line


def test_state_survives_a_round_trip_and_an_unreadable_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "agent-watch.json"
    states = {_DUTY.agent_id: WatchState(silent_since=1.0, last_wake_at=2.0)}
    write_state(path, states)
    assert read_state(path) == states

    path.write_text("not json", encoding="utf-8")
    assert read_state(path) == {}
