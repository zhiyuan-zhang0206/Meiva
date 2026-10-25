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
from concurrent.futures import ThreadPoolExecutor
from itertools import count
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.config import settings
from base.db import Database
from base.events.live.bus import EventBus, EventBusConfig
from base.events.live.tests.live_capture import snapshots_at_events
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from meiva_builtins.plugins.meiva_fleet.task_maintenance import daemon
from meiva_builtins.plugins.meiva_fleet.task_maintenance.daemon import run_reminders
from meiva_builtins.plugins.meiva_fleet.task_maintenance.tests.failing_writes import (
    inserts_failing,
    reminder_counter_failing,
)
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


def _open_notices(db: psycopg.Connection, agent_id: int) -> list[tuple[str, str, bool, int]]:
    """Open notices on an agent: (title, priority, require_response, task_id)."""
    db.rollback()  # the daemon committed on its own connection; refresh our view
    with db.cursor() as cur:
        cur.execute(
            "SELECT title, priority, require_response, task_id FROM agent_notices "
            "WHERE agent_id = %s AND resolved_at IS NULL ORDER BY local_id",
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


def _seed_notice(db: psycopg.Connection, agent_id: int) -> None:
    """Give an agent one pre-existing open FYI notice."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_notices (agent_id, local_id, title, priority, require_response, blocking, expire_at) "
            "VALUES (%s, 0, 'pre-existing', 'P2', FALSE, FALSE, now() + interval '1 day')",
            (agent_id,),
        )
    db.commit()


class TestRemind:
    def test_delivery_publishes_agent_updated(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
        db_url: str,
    ) -> None:
        owner = _make_agent(db_conn)

        def committed_inbound() -> int:
            with psycopg.connect(db_url) as separate, separate.cursor() as cur:
                cur.execute("SELECT count(*) FROM inbound_messages WHERE agent_id = %s", (owner,))
                row = cur.fetchone()
            assert row is not None
            return int(row[0])

        _make_task(db_conn, owner=owner, updated_s_ago=7200)
        with snapshots_at_events(event_bus, committed_inbound) as seen:
            assert daemon.run_reminders(pool, database, event_bus, 3600) == 1

        hints = [count for event, count in seen if event["role"] == "agent_updated"]
        assert hints == [1]
        assert _inbound_messages(db_conn, owner)[0][1:] == ("system_note", "system")

    def test_single_overdue_task_delivers_single_task_digest(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        emitted_events: EmittedEvents,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=3600)
        assert run_reminders(pool, database, event_bus, 3600.0) == 1
        assert len(deliver.deliveries()) == 1
        delivered_owner, message = deliver.deliveries()[0]
        assert delivered_owner == owner
        assert message.splitlines()[0] == (
            "Task check-in — 1 task(s) reached their reminder interval; "
            "check whether each needs a progress update or should be pushed forward:"
        )
        assert "no update for 1.0h" in message
        assert "if you cannot advance it, state why and adjust the reminder interval" in message
        assert "overdue" not in message
        assert f"#{tid}" in message
        assert _maintenance_events(emitted_events) == [
            (
                "telemetry",
                "task_reminder_digest",
                "info",
                owner,
                "system",
                {"owner_id": owner, "task_count": 1, "task_ids": [tid]},
            )
        ]
        row = _task_row(db_conn, tid)
        assert row is not None
        _status, _owner, reminder_count, last_reminded_at = row
        assert reminder_count == 1
        assert last_reminded_at is not None

    def test_owner_receives_one_digest_for_all_overdue_tasks(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        """A per-task send would deliver three messages and leave this digest absent."""
        owner = _make_agent(db_conn)
        task_ids = [
            _make_task(
                db_conn,
                owner=owner,
                title=f"overdue-{number}",
                remind_interval_seconds=1800,
                updated_s_ago=3600,
            )
            for number in range(3)
        ]

        assert run_reminders(pool, database, event_bus, 3600.0) == 1
        assert len(deliver.deliveries()) == 1
        delivered_owner, message = deliver.deliveries()[0]
        assert delivered_owner == owner
        assert "Task check-in — 3 task(s) reached their reminder interval" in message
        assert all(f"#{task_id}" in message for task_id in task_ids)
        for task_id in task_ids:
            row = _task_row(db_conn, task_id)
            assert row is not None
            assert row[2] == 1
            assert row[3] is not None

    def test_digest_lists_tasks_priority_first(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        """Overdue tasks in one digest are ordered P0 first (user ruling
        2026-08-29: the reminder orders by priority so the highest-stakes
        task leads the list), regardless of creation order."""
        owner = _make_agent(db_conn)
        p2 = _make_task(
            db_conn,
            owner=owner,
            title="low",
            priority="P2",
            remind_interval_seconds=1800,
            updated_s_ago=3600,
        )
        p0 = _make_task(
            db_conn,
            owner=owner,
            title="top",
            priority="P0",
            remind_interval_seconds=1800,
            updated_s_ago=3600,
        )
        p1 = _make_task(
            db_conn,
            owner=owner,
            title="mid",
            priority="P1",
            remind_interval_seconds=1800,
            updated_s_ago=3600,
        )

        assert run_reminders(pool, database, event_bus, 3600.0) == 1
        assert len(deliver.deliveries()) == 1
        _delivered_owner, message = deliver.deliveries()[0]
        ids_in_order = [int(line.split(" ")[1].lstrip("#")) for line in message.splitlines()[1:]]
        assert ids_in_order == [p0, p1, p2]
        # The imperative instruction rides each line.
        assert "if you cannot advance it, state why and adjust the reminder interval" in message
        assert "overdue" not in message
        # The reminder interval stays on the line.
        assert "reminder interval: 30min" in message

    def test_owners_receive_separate_digests(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        """Grouping all overdue tasks together would leak one owner's work to another."""
        first_owner = _make_agent(db_conn)
        second_owner = _make_agent(db_conn)
        _make_task(db_conn, owner=first_owner, remind_interval_seconds=1800, updated_s_ago=3600)
        _make_task(db_conn, owner=first_owner, remind_interval_seconds=1800, updated_s_ago=3600)
        _make_task(db_conn, owner=second_owner, remind_interval_seconds=1800, updated_s_ago=3600)

        assert run_reminders(pool, database, event_bus, 3600.0) == 2
        messages_by_owner = dict(deliver.deliveries())
        assert set(messages_by_owner) == {first_owner, second_owner}
        assert "2 task(s) reached their reminder interval" in messages_by_owner[first_owner]
        assert "1 task(s) reached their reminder interval" in messages_by_owner[second_owner]

    def test_not_yet_overdue_is_skipped(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        owner = _make_agent(db_conn)
        _make_task(db_conn, owner=owner, remind_interval_seconds=3600, updated_s_ago=1800)
        assert run_reminders(pool, database, event_bus, 3600.0) == 0
        assert deliver.deliveries() == []

    def test_terminated_owner_reminder_reroutes_without_resurrection(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        # meiva:note
        # A dead owner cannot act: the digest goes to its manager and the dead owner's
        # queue stays empty (2026-10-25 user ruling).
        manager = _make_agent(db_conn)
        parent = _make_task(db_conn, owner=manager, remind_interval_seconds=None)
        dead = _make_agent(db_conn, status="terminated")
        task_id = _make_task(
            db_conn,
            owner=dead,
            parent_id=parent,
            remind_interval_seconds=1800,
            updated_s_ago=3600,
        )

        assert run_reminders(pool, database, event_bus, 3600.0) == 1

        assert _inbound_messages(db_conn, dead) == []
        inbounds = _inbound_messages(db_conn, manager)
        assert len(inbounds) == 1
        content, kind, source = inbounds[0]
        assert kind == "system_note"
        assert source == "system"
        assert f"Agent #{dead} (terminated)" in content
        assert f'#{task_id} "' in content
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT payload FROM inbound_messages WHERE agent_id = %s AND kind = 'system_note'",
                (manager,),
            )
            payload = cur.fetchone()
            assert payload is not None and payload[0] == {
                "note_tag": "task",
                "task_notification": True,
                "delivery_resurrect": False,
            }
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (dead,))
            assert cur.fetchone() == ("terminated",)
        row = _task_row(db_conn, task_id)
        assert row is not None
        _status, _owner, reminder_count, last_reminded_at = row
        assert reminder_count == 1
        assert last_reminded_at is not None

    def test_acceptance_failure_leaves_counters_untouched(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        """A failed delivery does not bump last_reminded_at / reminder_count, so the
        task is retried on the next sweep."""
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=3600)

        with (
            inserts_failing(db_conn, "inbound_messages"),
            pytest.raises(psycopg.errors.RaiseException, match="inbound_messages inserts refused"),
        ):
            run_reminders(pool, database, event_bus, 3600.0)
        row = _task_row(db_conn, tid)
        assert row is not None
        _status, _owner, reminder_count, last_reminded_at = row
        assert reminder_count == 0
        assert last_reminded_at is None

    def test_null_remind_interval_is_skipped(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        owner = _make_agent(db_conn)
        _make_task(db_conn, owner=owner, remind_interval_seconds=None, updated_s_ago=100 * _DAY_S)
        assert run_reminders(pool, database, event_bus, 3600.0) == 0

    def test_done_task_not_reminded(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        """Only in_progress tasks are reminded (post-2026-08-29 the 'open'
        status no longer exists — a done task is the non-reminded case)."""
        owner = _make_agent(db_conn)
        _make_task(
            db_conn, status="done", owner=owner, remind_interval_seconds=1800, updated_s_ago=3600
        )
        assert run_reminders(pool, database, event_bus, 3600.0) == 0

    # meiva:note
    # A domain container is a roll-up, not a work item: while it has an open direct child, its own
    # overdue window never produces a digest.
    def test_parent_with_open_child_is_not_a_reminder_candidate(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        owner = _make_agent(db_conn)
        parent = _make_task(db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=7200)
        _make_task(db_conn, owner=owner, parent_id=parent, updated_s_ago=0)

        assert run_reminders(pool, database, event_bus, 3600.0) == 0
        assert deliver.deliveries() == []

    def test_overdue_child_under_a_parent_reminds_on_its_own(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        owner = _make_agent(db_conn)
        parent = _make_task(db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=7200)
        child = _make_task(
            db_conn,
            owner=owner,
            parent_id=parent,
            remind_interval_seconds=1800,
            updated_s_ago=7200,
        )

        assert run_reminders(pool, database, event_bus, 3600.0) == 1
        assert len(deliver.deliveries()) == 1
        message = deliver.deliveries()[0][1]
        assert f'- #{child} "' in message
        assert f'- #{parent} "' not in message
        parent_row = _task_row(db_conn, parent)
        assert parent_row is not None
        # meiva:note
        # The parent's cadence stays untouched: it is not reminded, so only the candidate's
        # counters advance.
        assert parent_row[2] == 0
        assert parent_row[3] is None

    # meiva:note
    # Skipping containers is not a permanent mute: once no direct child is open, the parent is a
    # work item again and its overdue window counts.
    @pytest.mark.parametrize("child_status", ["done", "cancelled"])
    def test_parent_returns_to_candidates_after_children_close(
        self,
        child_status: str,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        owner = _make_agent(db_conn)
        parent = _make_task(db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=7200)
        _make_task(
            db_conn,
            status=child_status,
            owner=owner,
            parent_id=parent,
            updated_s_ago=0,
        )

        assert run_reminders(pool, database, event_bus, 3600.0) == 1
        assert f'- #{parent} "' in deliver.deliveries()[0][1]

    def test_task_without_children_still_reminds(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        owner = _make_agent(db_conn)
        leaf = _make_task(db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=3600)

        assert run_reminders(pool, database, event_bus, 3600.0) == 1
        assert f'- #{leaf} "' in deliver.deliveries()[0][1]

    # meiva:note
    # The skip is by direct child, so a two-level container chain drops both container levels and
    # leaves only the leaf as a candidate.
    def test_grandparent_with_open_container_child_is_not_a_candidate(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        owner = _make_agent(db_conn)
        grandparent = _make_task(
            db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=7200
        )
        container = _make_task(
            db_conn,
            owner=owner,
            parent_id=grandparent,
            remind_interval_seconds=1800,
            updated_s_ago=7200,
        )
        leaf = _make_task(
            db_conn,
            owner=owner,
            parent_id=container,
            remind_interval_seconds=1800,
            updated_s_ago=7200,
        )

        assert run_reminders(pool, database, event_bus, 3600.0) == 1
        message = deliver.deliveries()[0][1]
        assert f'- #{leaf} "' in message
        assert f'- #{container} "' not in message
        assert f'- #{grandparent} "' not in message

    def test_backoff_blocks_duplicate_reminder(
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
            last_reminded_s_ago=1800,  # reminded 30 min ago, backoff is 1h
        )
        assert run_reminders(pool, database, event_bus, 3600.0) == 0

    def test_backoff_expired_allows_new_reminder(
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
            last_reminded_s_ago=7200,  # reminded 2h ago, backoff is 1h
        )
        assert run_reminders(pool, database, event_bus, 3600.0) == 1

    def test_interval_floor_blocks_hourly_nag(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        """A task whose remind_interval exceeds the backoff floor repeats at its
        own interval: a P3 task (4h) reminded 1.5h ago is NOT nagged again,
        even though the 1h backoff has elapsed (pre-floor behavior would
        remind it every hour once overdue)."""
        owner = _make_agent(db_conn)
        _make_task(
            db_conn,
            owner=owner,
            remind_interval_seconds=14400,
            updated_s_ago=20000,
            last_reminded_s_ago=5400,  # 1.5h ago
        )
        assert run_reminders(pool, database, event_bus, 3600.0) == 0
        assert deliver.deliveries() == []

    def test_interval_floor_allows_repeat_after_full_interval(
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
            remind_interval_seconds=14400,
            updated_s_ago=20000,
            last_reminded_s_ago=15000,  # 4h+ elapsed — a fresh reminder is due
        )
        assert run_reminders(pool, database, event_bus, 3600.0) == 1

    def test_counter_failure_rolls_back_digest_and_all_counters(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        deliver: TaskNotes,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        """A crash after insertion cannot leave only half the reminder committed."""
        owner = _make_agent(db_conn)
        task_ids = [
            _make_task(db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=3600)
            for _ in range(2)
        ]
        with (
            reminder_counter_failing(db_conn, task_ids[1]),
            pytest.raises(psycopg.errors.RaiseException, match="reminder counter refused"),
        ):
            run_reminders(pool, database, event_bus, 3600.0)
        assert deliver.deliveries() == []
        with db_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (owner,))
            assert cur.fetchone() == (0,)
        for task_id in task_ids:
            row = _task_row(db_conn, task_id)
            assert row is not None and row[2:] == (0, None)
        assert run_reminders(pool, database, event_bus, 3600.0) == 1
        assert len(deliver.deliveries()) == 1
        assert run_reminders(pool, database, event_bus, 3600.0) == 0

    def test_committed_reminder_survives_lost_live_hint(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        owner = _make_agent(db_conn)
        tid = _make_task(db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=3600)
        unreachable_bus = EventBus(
            EventBusConfig(redis_url="redis://127.0.0.1:1/0", events_channel="lost-hints")
        )

        assert run_reminders(pool, database, unreachable_bus, 3600.0) == 1
        assert run_reminders(pool, database, unreachable_bus, 3600.0) == 0
        row = _task_row(db_conn, tid)
        assert row is not None and row[2] == 1
        with db_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (owner,))
            assert cur.fetchone() == (1,)


def test_concurrent_reminder_sweeps_accept_one_cadence(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    deliver: TaskNotes,
) -> None:
    owner = _make_agent(db_conn)
    task_id = _make_task(db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=3600)
    with ThreadPoolExecutor(max_workers=2) as workers:
        attempts = [
            workers.submit(run_reminders, pool, database, event_bus, 3600.0) for _ in range(2)
        ]
        assert sorted(attempt.result(timeout=10) for attempt in attempts) == [0, 1]
    assert len(_inbound_messages(db_conn, owner)) == 1
    row = _task_row(db_conn, task_id)
    assert row is not None and row[2] == 1
    assert len(deliver.deliveries()) == 1
