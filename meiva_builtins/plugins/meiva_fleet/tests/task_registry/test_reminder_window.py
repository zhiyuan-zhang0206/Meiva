# meiva:note
# Which writes restart a task's reminder ladder, per update kind, end to end through the SDK entry
# point. `reminder_count` is what the task-maintenance daemon escalates on, and the default policy
# counts the work (status, results, owner, priority, parent) and a note, while leaving a rename, a
# description edit and an interval change out. A note counts by user ruling (2026-10-31): an owner
# answering a reminder with one is still on the task, and reading it as silence escalated tasks
# nobody had abandoned.
# meiva:link meiva_builtins/plugins/meiva_fleet/docs/tasks/task_model/task_model_reminders.meiva.okf.md

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import psycopg
import pytest

from base.packages.plugins.config_registration import disk_image_path
from base.paths.tests.homes import unit_home as unit_home
from meiva_builtins.plugins.meiva_fleet import task_registry
from meiva_builtins.plugins.meiva_fleet.default_config import FleetConfig
from meiva_builtins.plugins.meiva_fleet.tests.task_registry.seeds import (
    root_task_id as root_task_id,
)
from meiva_builtins.plugins.meiva_fleet.tests.task_registry.seeds import (
    seed_agent,
    seed_owned_task,
)

Ladder = tuple[int, object, object]


# meiva:note
# Put the task where two delivered reminders leave it, escalation marker included.
def _climb(db: psycopg.Connection, task_id: int) -> None:
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agent_tasks SET reminder_count = 2, last_reminded_at = now(), "
            "escalated_at = now() WHERE id = %s",
            (task_id,),
        )
    db.commit()


def _ladder(db: psycopg.Connection, task_id: int) -> Ladder:
    with db.cursor() as cur:
        cur.execute(
            "SELECT reminder_count, last_reminded_at, escalated_at FROM agent_tasks WHERE id = %s",
            (task_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0]), row[1], row[2]


def _restarted(db: psycopg.Connection, task_id: int) -> None:
    count, reminded_at, escalated_at = _ladder(db, task_id)
    assert (count, reminded_at, escalated_at) == (0, None, None)


def _kept(db: psycopg.Connection, task_id: int, climbed: Ladder) -> None:
    assert _ladder(db, task_id) == climbed


def _climbed(db: psycopg.Connection, task_id: int) -> Ladder:
    _climb(db, task_id)
    climbed = _ladder(db, task_id)
    assert climbed[0] == 2
    return climbed


# meiva:note
# The user's bug report (2026-10-31): answering a reminder with a note left the counters climbing,
# so an owner who reported in was escalated as if nobody had come back.
def test_note_only_write_restarts_the_ladder(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    _actor, _owner, task_id = seed_owned_task(db_conn, root_task_id)
    _climb(db_conn, task_id)
    task_registry.update(task_id, note="still waiting on the draft", operation_key=str(uuid4()))
    _restarted(db_conn, task_id)


# meiva:note
# A retry of the same write is not a second sign of life: the keyed replay must leave the window
# where the first delivery put it, or a caller that retries could hold a task fresh forever.
def test_a_replayed_note_does_not_advance_the_window_again(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    _actor, _owner, task_id = seed_owned_task(db_conn, root_task_id)
    task_registry.update(task_id, note="first and only", operation_key="replayed")
    _age(db_conn, task_id)
    climbed = _climbed(db_conn, task_id)
    task_registry.update(task_id, note="first and only", operation_key="replayed")
    _kept(db_conn, task_id, climbed)
    assert _age_days(db_conn, task_id) > 29


def test_results_write_restarts_the_ladder(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    _actor, _owner, task_id = seed_owned_task(db_conn, root_task_id)
    _climb(db_conn, task_id)
    task_registry.update(task_id, results="came back clean", operation_key=str(uuid4()))
    _restarted(db_conn, task_id)


# meiva:note
# A restatement is not progress: the second identical write must not restart anything.
def test_restating_the_same_results_keeps_the_ladder(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    _actor, _owner, task_id = seed_owned_task(db_conn, root_task_id)
    task_registry.update(task_id, results="came back clean", operation_key=str(uuid4()))
    climbed = _climbed(db_conn, task_id)
    task_registry.update(task_id, results="came back clean", operation_key=str(uuid4()))
    _kept(db_conn, task_id, climbed)


def test_status_change_restarts_the_ladder(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    _actor, _owner, task_id = seed_owned_task(db_conn, root_task_id)
    _climb(db_conn, task_id)
    task_registry.update(task_id, status="done", operation_key=str(uuid4()))
    _restarted(db_conn, task_id)


def test_title_change_keeps_the_ladder(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    _actor, _owner, task_id = seed_owned_task(db_conn, root_task_id)
    climbed = _climbed(db_conn, task_id)
    task_registry.update(task_id, title="receipt task, renamed", operation_key=str(uuid4()))
    _kept(db_conn, task_id, climbed)


def test_description_change_keeps_the_ladder(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    _actor, _owner, task_id = seed_owned_task(db_conn, root_task_id)
    climbed = _climbed(db_conn, task_id)
    task_registry.update(task_id, description="a sharper brief", operation_key=str(uuid4()))
    _kept(db_conn, task_id, climbed)


def test_priority_change_restarts_the_ladder(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    _actor, _owner, task_id = seed_owned_task(db_conn, root_task_id)
    _climb(db_conn, task_id)
    task_registry.update(task_id, priority="P0", operation_key=str(uuid4()))
    _restarted(db_conn, task_id)


# meiva:note
# A handover gives the new owner a fresh window.
def test_owner_change_restarts_the_ladder(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    _actor, _owner, task_id = seed_owned_task(db_conn, root_task_id)
    successor = seed_agent(db_conn)
    _climb(db_conn, task_id)
    task_registry.update(task_id, owner=successor, operation_key=str(uuid4()))
    _restarted(db_conn, task_id)


# meiva:note
# A new parent means a new escalation target, so the new delegator starts a fresh window.
def test_reparent_restarts_the_ladder(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    _actor, _owner, task_id = seed_owned_task(db_conn, root_task_id)
    other_parent = task_registry.create(
        "another parent", "container", parent=root_task_id, operation_key=str(uuid4())
    )
    _climb(db_conn, task_id)
    task_registry.update(task_id, parent_id=other_parent.id, operation_key=str(uuid4()))
    _restarted(db_conn, task_id)


# meiva:note
# Parking a task is not progress: `reminders_paused_until` is written to its own column, so it must
# not touch the ladder it exists to silence, and must not move the due clock either (task #5311).
def _pause_policy(unit_home: Path, max_seconds: int) -> None:
    image = disk_image_path("meiva_fleet", home=unit_home)
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_text(
        FleetConfig(task_reminder_pause_max_seconds=max_seconds).model_dump_json(), encoding="utf-8"
    )


def _paused_until(db: psycopg.Connection, task_id: int) -> datetime | None:
    with db.cursor() as cur:
        cur.execute("SELECT reminders_paused_until FROM agent_tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
    assert row is not None
    return cast("datetime | None", row[0])


def test_pause_keeps_the_ladder_and_the_clock(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int, unit_home: Path
) -> None:
    _pause_policy(unit_home, 2 * 86400)
    _actor, _owner, task_id = seed_owned_task(db_conn, root_task_id)
    _age(db_conn, task_id)
    climbed = _climbed(db_conn, task_id)
    deadline = datetime.now(UTC) + timedelta(hours=1)
    task_registry.update(task_id, reminders_paused_until=deadline, operation_key=str(uuid4()))
    _kept(db_conn, task_id, climbed)
    assert _age_days(db_conn, task_id) > 29
    stored = _paused_until(db_conn, task_id)
    assert stored is not None
    assert abs((stored - deadline).total_seconds()) < 1


def test_pause_past_the_bound_is_rejected(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int, unit_home: Path
) -> None:
    _pause_policy(unit_home, 3600)
    _actor, _owner, task_id = seed_owned_task(db_conn, root_task_id)
    with pytest.raises(ValueError, match="task_reminder_pause_max_seconds"):
        task_registry.update(
            task_id,
            reminders_paused_until=datetime.now(UTC) + timedelta(days=1),
            operation_key=str(uuid4()),
        )
    assert _paused_until(db_conn, task_id) is None


# meiva:note
# A home that never chose a bound cannot park anything: the request fails and names the key, rather
# than a default nobody decided.
def test_pause_needs_a_bound_on_this_home(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    _actor, _owner, task_id = seed_owned_task(db_conn, root_task_id)
    with pytest.raises(ValueError, match="cannot pause task reminders"):
        task_registry.update(
            task_id,
            reminders_paused_until=datetime.now(UTC) + timedelta(hours=1),
            operation_key=str(uuid4()),
        )


# meiva:note
# The column is the deadline itself, so a past one means "not paused": resuming needs no second verb
# and no second flag.
def test_a_past_deadline_resumes(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int, unit_home: Path
) -> None:
    _pause_policy(unit_home, 2 * 86400)
    _actor, _owner, task_id = seed_owned_task(db_conn, root_task_id)
    climbed = _climbed(db_conn, task_id)
    task_registry.update(
        task_id,
        reminders_paused_until=datetime.now(UTC) - timedelta(minutes=1),
        operation_key=str(uuid4()),
    )
    _kept(db_conn, task_id, climbed)
    stored = _paused_until(db_conn, task_id)
    assert stored is not None
    assert stored < datetime.now(UTC)


def test_policy_can_leave_the_note_out(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int, unit_home: Path
) -> None:
    # meiva:note
    # The documented narrow window: a home that wants escalation to measure progress alone leaves
    # `note` out. The policy is read from this home's `meiva_fleet` image on every write, so writing
    # the image is the whole switch — no restart, no patched symbol. This is also the one path
    # where a note-only write carries no row field at all, so the row UPDATE must be skipped.
    image = disk_image_path("meiva_fleet", home=unit_home)
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_text(
        FleetConfig(task_reminder_reset_on=("status", "results")).model_dump_json(),
        encoding="utf-8",
    )
    _actor, _owner, task_id = seed_owned_task(db_conn, root_task_id)
    climbed = _climbed(db_conn, task_id)
    task_registry.update(task_id, note="explicit progress", operation_key=str(uuid4()))
    _kept(db_conn, task_id, climbed)


# meiva:note
# `updated_at` is the reminder daemon's due clock (`now() - updated_at > remind_interval_seconds`),
# so one criterion decides both: it moves exactly when the window restarts. The default counts a
# note, so answering a reminder with a log line postpones the next one rather than letting the
# ladder climb to escalation.
def _age(db: psycopg.Connection, task_id: int) -> None:
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agent_tasks SET updated_at = now() - interval '30 days' WHERE id = %s",
            (task_id,),
        )
    db.commit()


def _age_days(db: psycopg.Connection, task_id: int) -> float:
    with db.cursor() as cur:
        cur.execute(
            "SELECT EXTRACT(EPOCH FROM (now() - updated_at)) / 86400 FROM agent_tasks WHERE id = %s",
            (task_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return float(row[0])


def test_note_only_write_moves_the_clock(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    _actor, _owner, task_id = seed_owned_task(db_conn, root_task_id)
    _age(db_conn, task_id)
    task_registry.update(task_id, note="still waiting on the draft", operation_key=str(uuid4()))
    assert _age_days(db_conn, task_id) < 1


def test_results_write_moves_the_clock(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    _actor, _owner, task_id = seed_owned_task(db_conn, root_task_id)
    _age(db_conn, task_id)
    task_registry.update(task_id, results="came back clean", operation_key=str(uuid4()))
    assert _age_days(db_conn, task_id) < 1


# meiva:note
# The clock follows the policy, not "any field": a rename restates the work, so it keeps the age.
def test_title_change_keeps_the_clock(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    _actor, _owner, task_id = seed_owned_task(db_conn, root_task_id)
    _age(db_conn, task_id)
    task_registry.update(task_id, title="receipt task, renamed", operation_key=str(uuid4()))
    assert _age_days(db_conn, task_id) > 29


# meiva:note
# Why a task did or did not escalate must be readable after the fact.
def test_audit_fact_records_the_restart(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    _actor, _owner, task_id = seed_owned_task(db_conn, root_task_id)
    _climb(db_conn, task_id)
    task_registry.update(task_id, results="came back clean", operation_key=str(uuid4()))
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT attributes FROM audit_events WHERE event_name = 'task_update' "
            "AND attributes->>'task_id' = %s ORDER BY id DESC LIMIT 1",
            (str(task_id),),
        )
        row = cur.fetchone()
    assert row is not None
    # meiva:note
    # The audit row carries its own JSON payload; the columns are the console's view of it.
    payload = cast("dict[str, object]", row[0])
    assert payload["reminder_window_reset"] is True
    assert payload["results_replaced"] is True
