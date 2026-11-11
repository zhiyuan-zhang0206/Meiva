# meiva:note
# `plugins.meiva_fleet.task_maintenance.daemon` — task reminders and escalation, cluster-wide and
# gateway-owned. `run_reminders` finds in-progress tasks past their remind_interval_seconds and
# commits one digest per owner as a direct inbound insert together with the counters; terminated
# owners keep their inbox row without being revived, and each overdue window gets at most one
# reminder per backoff period. `run_escalate` notifies the parent task's owner when reminder_count
# reaches the escalation threshold. The tests run both against the throwaway Postgres and Redis and
# read the delivered digests back from the inbound rows. No stale sweep, no automatic cancellation:
# rows are UPDATEd, never DELETEd.

from __future__ import annotations

from collections.abc import Iterator
from itertools import count
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.config import get_field, settings
from base.daemon.endpoints import ServiceEndpoints
from base.db import Database
from base.events.live.bus import EventBus
from base.paths import meiva_home, run_dir
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from meiva_builtins.plugins.meiva_fleet.task_maintenance import daemon
from meiva_builtins.plugins.meiva_fleet.task_maintenance.daemon import run_escalate, run_reminders
from meiva_builtins.plugins.meiva_fleet.tests.task_registry.notes import TaskNotes, record_notes

_DAY_S = 86400.0


@pytest.fixture
def pool(db_url: str):
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def deliver(db_conn: psycopg.Connection) -> Iterator[TaskNotes]:
    with record_notes(db_conn) as notes:
        yield notes


# meiva:note
# The health-endpoint table a plugin's `services()` hook is handed: this checkout's reader and home.
def _table() -> ServiceEndpoints:
    return ServiceEndpoints.from_fields(get_field, runtime=run_dir(meiva_home()))


def _maintenance_events(
    events: EmittedEvents,
) -> list[tuple[str, str, str, int | None, str, dict[str, Any]]]:
    """What the maintenance daemon emitted: category, name, level, agent, source and attributes."""
    return [(e.category, e.event_name, e.level, e.agent_id, e.source, e.attributes) for e in events]


def _make_agent(db: psycopg.Connection, *, status: str = "running") -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        aid = int(cur.fetchone()[0])  # type: ignore[index]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', %s)",
            (aid, status),
        )
    db.commit()
    return aid


_TASK_TITLE = count(1)


def _make_task(
    db: psycopg.Connection,
    *,
    status: str = "in_progress",
    owner: int | None = None,
    parent_id: int | None = None,
    updated_s_ago: float = 0.0,
    remind_interval_seconds: int | None = 1800,
    last_reminded_s_ago: float | None = None,
    reminder_count: int = 0,
    priority: str = "P2",
    title: str | None = None,
) -> int:
    # Distinct titles by default: the agent_tasks partial unique index forbids
    # two in_progress rows sharing a title, and these tests create many.
    if title is None:
        title = f"t-{next(_TASK_TITLE)}"
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, status, owner, created_by, "
            "parent_id, remind_interval_seconds, priority) "
            "VALUES (%s, 'd', %s, %s, 'user', %s, %s, %s) RETURNING id",
            (title, status, owner, parent_id, remind_interval_seconds, priority),
        )
        tid = int(cur.fetchone()[0])  # type: ignore[index]
        cur.execute(
            "UPDATE agent_tasks SET updated_at = now() - make_interval(secs => %s) WHERE id = %s",
            (updated_s_ago, tid),
        )
        if last_reminded_s_ago is not None:
            cur.execute(
                "UPDATE agent_tasks SET last_reminded_at = now() - make_interval(secs => %s) "
                "WHERE id = %s",
                (last_reminded_s_ago, tid),
            )
        if reminder_count:
            cur.execute(
                "UPDATE agent_tasks SET reminder_count = %s WHERE id = %s",
                (reminder_count, tid),
            )
    db.commit()
    return tid


def _task_row(db: psycopg.Connection, tid: int) -> tuple[Any, ...] | None:
    with db.cursor() as cur:
        cur.execute(
            "SELECT status, owner, reminder_count, last_reminded_at FROM agent_tasks WHERE id = %s",
            (tid,),
        )
        return cur.fetchone()


# meiva:note
# Park the task until `now() + seconds_ahead`; a negative value is a deadline that already passed.
def _pause(db: psycopg.Connection, tid: int, *, seconds_ahead: float) -> None:
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agent_tasks SET reminders_paused_until = now() + make_interval(secs => %s) "
            "WHERE id = %s",
            (seconds_ahead, tid),
        )
    db.commit()


def _open_notices(db: psycopg.Connection, agent_id: int) -> list[tuple[str]]:
    db.rollback()  # the daemon committed on its own connection; refresh our view
    with db.cursor() as cur:
        cur.execute(
            "SELECT title FROM agent_notices WHERE agent_id = %s AND resolved_at IS NULL ORDER BY id",
            (agent_id,),
        )
        return cur.fetchall()


def _inbound_messages(db: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str]]:
    """Inbound rows for an agent: (content, kind, source)."""
    db.rollback()  # the daemon committed on its own connection; refresh our view
    with db.cursor() as cur:
        cur.execute(
            "SELECT content, kind, source FROM inbound_messages WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        return cur.fetchall()


class TestEscalate:
    def test_delegator_receives_one_digest_for_all_stalled_subtasks(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        emitted_events: EmittedEvents,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        """Per-subtask escalation would produce two chats instead of one digest."""
        delegator = _make_agent(db_conn)
        parent = _make_task(db_conn, owner=delegator, remind_interval_seconds=None)
        first_owner = _make_agent(db_conn)
        second_owner = _make_agent(db_conn)
        task_ids = [
            _make_task(
                db_conn,
                owner=owner,
                parent_id=parent,
                remind_interval_seconds=1800,
                updated_s_ago=7200,
                reminder_count=3,
            )
            for owner in (first_owner, second_owner)
        ]

        assert run_escalate(pool, database, event_bus, 3, 0.0) == 1
        assert len(deliver.deliveries()) == 1
        delivered_owner, message = deliver.deliveries()[0]
        assert delivered_owner == delegator
        assert (
            "Subtasks still open with no state change "
            "(a note or a change on the task would have restarted the reminders):"
        ) in message
        assert all(f"#{task_id}" in message for task_id in task_ids)
        # _ESCALATE_SQL sorts nothing: row order is unspecified, and neither the
        # digest message (checked membership-wise above) nor the telemetry makes
        # an ordering claim — assert the id set, not the SQL's incidental row
        # order. Exact-list equality here was a shard7 flake (task #2199).
        assert _maintenance_events(emitted_events) == [
            (
                "telemetry",
                "task_escalation",
                "info",
                delegator,
                "system",
                {
                    "owner_id": delegator,
                    "task_count": 2,
                    "task_ids": sorted(task_ids),
                    "leg": "delegator",
                },
            )
        ]

    def test_escalates_at_threshold(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        parent_owner = _make_agent(db_conn)
        parent = _make_task(db_conn, owner=parent_owner, remind_interval_seconds=None)
        owner = _make_agent(db_conn)
        _make_task(
            db_conn,
            owner=owner,
            parent_id=parent,
            remind_interval_seconds=1800,
            updated_s_ago=7200,
            reminder_count=3,
        )
        assert run_escalate(pool, database, event_bus, 3, 0.0) == 1
        assert len(deliver.deliveries()) == 1
        delivered_owner, message = deliver.deliveries()[0]
        assert delivered_owner == parent_owner
        assert "no state change for 2.0h" in message

    # meiva:note
    # The reminder skip is scoped to the reminder candidate query: a task with an open child that
    # is past the escalation threshold still escalates to its own parent owner, while the reminder
    # pass leaves the same container alone.
    def test_container_still_escalates_for_its_own_parent_owner(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        delegator = _make_agent(db_conn)
        grandparent = _make_task(db_conn, owner=delegator, remind_interval_seconds=None)
        stalled = _make_agent(db_conn)
        container = _make_task(
            db_conn,
            owner=stalled,
            parent_id=grandparent,
            remind_interval_seconds=1800,
            updated_s_ago=7200,
            reminder_count=3,
        )
        _make_task(db_conn, owner=stalled, parent_id=container, updated_s_ago=0)

        assert run_escalate(pool, database, event_bus, 3, 0.0) == 1
        assert len(deliver.deliveries()) == 1
        delivered_owner, message = deliver.deliveries()[0]
        assert delivered_owner == delegator
        assert f"#{container}" in message

        deliver.restart()
        assert daemon.run_reminders(pool, database, event_bus, 3600.0) == 0
        assert deliver.deliveries() == []

    def test_a_short_silence_is_skipped(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        # meiva:note
        # The threshold counts cadence periods, so `escalate_n=3` over a 1800s cadence
        # needs 5400s of silence. The reminder count does not gate it: one delivered
        # reminder is enough, two are not required (2026-11-07, task #6354).
        parent_owner = _make_agent(db_conn)
        parent = _make_task(db_conn, owner=parent_owner, remind_interval_seconds=None)
        owner = _make_agent(db_conn)
        _make_task(
            db_conn,
            owner=owner,
            parent_id=parent,
            remind_interval_seconds=1800,
            updated_s_ago=1800,
            reminder_count=3,
        )
        assert run_escalate(pool, database, event_bus, 3, 0.0) == 0
        assert deliver.deliveries() == []

    def test_above_threshold_escalates_once_per_window(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        # meiva:note
        # reminder_count=5 > threshold=3 with no marker: still one digest.
        parent_owner = _make_agent(db_conn)
        parent = _make_task(db_conn, owner=parent_owner, remind_interval_seconds=None)
        owner = _make_agent(db_conn)
        _make_task(
            db_conn,
            owner=owner,
            parent_id=parent,
            remind_interval_seconds=1800,
            updated_s_ago=7200,
            reminder_count=5,
        )
        assert run_escalate(pool, database, event_bus, 3, 0.0) == 1
        assert len(deliver.deliveries()) == 1

    def test_no_parent_no_escalation(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        owner = _make_agent(db_conn)
        _make_task(
            db_conn,
            owner=owner,
            remind_interval_seconds=1800,
            updated_s_ago=7200,
            reminder_count=3,
        )
        assert run_escalate(pool, database, event_bus, 3, 0.0) == 0
        assert deliver.deliveries() == []

    # meiva:note
    # A stalled top-level task whose parent is ownerless (the system root) has no delegator to
    # catch it, and nobody else can act: the pass writes no notice, no chat message and no
    # telemetry for it, does not raise, and a later sweep finds it again and does the same.
    def test_task_with_no_live_manager_is_skipped_without_effects(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        emitted_events: EmittedEvents,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        root = _make_task(db_conn, owner=None, remind_interval_seconds=None)
        owner = _make_agent(db_conn)
        _make_task(
            db_conn,
            owner=owner,
            parent_id=root,
            remind_interval_seconds=1800,
            updated_s_ago=7200,
            reminder_count=5,
            priority="P1",
        )
        assert run_escalate(pool, database, event_bus, 3, 0.0) == 0
        assert run_escalate(pool, database, event_bus, 3, 0.0) == 0
        assert deliver.deliveries() == []
        assert _open_notices(db_conn, owner) == []
        assert _inbound_messages(db_conn, owner) == []
        assert _maintenance_events(emitted_events) == []


def test_service_command_module_is_importable() -> None:
    """The canonical root service command resolves after plugin relocation."""
    import importlib.util

    from meiva_builtins.plugins.meiva_fleet.services import services

    service = next(
        item
        for item in services(home=meiva_home(), endpoints=_table())
        if item.session == "task-maintenance"
    )
    module = service.cmd.split(" -m ", 1)[1]
    assert importlib.util.find_spec(module) is not None
    assert module.startswith("meiva_builtins.plugins.")


# meiva:note
# A parked task is silenced by both passes, and the deadline is the only thing that stops it: the
# pause neither clears the counters nor moves `updated_at`, so the same task is due again the moment
# the deadline passes. The escalation leg honours the same deadline, so a task that already reached
# the threshold is not escalated to its delegator while it is silenced (task #5311).
class TestPausedTasks:
    def test_a_paused_task_is_not_reminded(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=7200)
        _pause(db_conn, tid, seconds_ahead=3600)

        assert run_reminders(pool, database, event_bus, 3600.0) == 0
        assert deliver.deliveries() == []

    def test_an_expired_pause_is_due_again(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=7200)
        _pause(db_conn, tid, seconds_ahead=-60)

        assert run_reminders(pool, database, event_bus, 3600.0) == 1
        assert deliver.deliveries()[0][0] == owner

    def test_a_paused_task_is_not_escalated(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        parent_owner = _make_agent(db_conn)
        parent = _make_task(db_conn, owner=parent_owner, remind_interval_seconds=None)
        owner = _make_agent(db_conn)
        tid = _make_task(
            db_conn,
            owner=owner,
            parent_id=parent,
            remind_interval_seconds=1800,
            updated_s_ago=7200,
            reminder_count=3,
        )
        _pause(db_conn, tid, seconds_ahead=3600)

        assert run_escalate(pool, database, event_bus, 3, 0.0) == 0
        assert deliver.deliveries() == []
