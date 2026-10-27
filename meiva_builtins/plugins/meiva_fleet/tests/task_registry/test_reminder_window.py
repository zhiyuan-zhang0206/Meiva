# meiva:note
# Only a write that changes the work restarts a task's reminder ladder (task #5314).
# `reminder_count` is what the task-maintenance daemon escalates on, so a note-only write that
# zeroed it kept every task in the fleet at one reminder (87% of all updates are notes). These
# cases pin the criterion per update kind, end to end through the SDK entry point.
# meiva:link meiva_builtins/plugins/meiva_fleet/docs/tasks/task_model/task_model_reminders.meiva.okf.md

from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import uuid4

import psycopg

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
# The defect itself: a daily "no change" note must not rewind the counters.
def test_note_only_write_keeps_the_ladder(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    _actor, _owner, task_id = seed_owned_task(db_conn, root_task_id)
    climbed = _climbed(db_conn, task_id)
    task_registry.update(task_id, note="still waiting on the draft", operation_key=str(uuid4()))
    _kept(db_conn, task_id, climbed)


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


def test_policy_can_name_the_note_kind(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int, unit_home: Path
) -> None:
    # meiva:note
    # The documented rollback: naming `note` restores the rule that any write restarts. The policy
    # is read from this home's `meiva_fleet` image on every write, so writing the image is the
    # whole switch — no restart, no patched symbol.
    image = disk_image_path("meiva_fleet", home=unit_home)
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_text(
        FleetConfig(task_reminder_reset_on=("status", "note")).model_dump_json(), encoding="utf-8"
    )
    _actor, _owner, task_id = seed_owned_task(db_conn, root_task_id)
    _climb(db_conn, task_id)
    task_registry.update(task_id, note="explicit progress", operation_key=str(uuid4()))
    _restarted(db_conn, task_id)


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
