from __future__ import annotations

from itertools import count
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.config import settings
from base.db import Database
from base.events.live.bus import EventBus
from meiva_builtins.plugins.meiva_fleet.task_maintenance.daemon import run_escalate
from meiva_builtins.plugins.meiva_fleet.task_maintenance.tests.failing_writes import inserts_failing

# meiva:note
# Delegator escalation marker (`escalated_at`), plus the copy both legs deliver. The
# delegation leg reports a task still open with no state change by sending one
# digest to the delegator; the marker, stamped in the same transaction as that
# digest's inbound insert, makes the message at-most-once per overdue window — a
# failed delivery leaves it unset (the next sweep retries), and both update() paths
# clear it with the reminder counters (re-arming the task's next window). The copy
# asserts reminders and an unchanged task state, never a verdict on the owner: this
# pass reads task bookkeeping only, and a note restarts the window, so N reminders in a
# row mean nothing was recorded on the task.
# meiva:link meiva_builtins/plugins/meiva_fleet/task_maintenance/docs/task-maintenance/escalation.md
_TASK_TITLE = count(1)


@pytest.fixture
def pool(db_url: str):
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield p
    finally:
        p.close()


def make_agent(db: psycopg.Connection, *, status: str = "running") -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        aid = int(cur.fetchone()[0])  # type: ignore[index]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', %s)",
            (aid, status),
        )
    db.commit()
    return aid


def make_task(
    db: psycopg.Connection,
    *,
    owner: int | None,
    parent_id: int | None = None,
    remind_interval_seconds: int | None = 1800,
    updated_s_ago: float = 0.0,
    reminder_count: int = 0,
    title: str | None = None,
    priority: str = "P2",
) -> int:
    # Distinct titles by default: the agent_tasks partial unique index forbids
    # two in_progress rows sharing a title, and these tests create many.
    if title is None:
        title = f"t-{next(_TASK_TITLE)}"
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, status, owner, created_by, "
            "parent_id, remind_interval_seconds, priority) "
            "VALUES (%s, 'd', 'in_progress', %s, 'user', %s, %s, %s) RETURNING id",
            (title, owner, parent_id, remind_interval_seconds, priority),
        )
        tid = int(cur.fetchone()[0])  # type: ignore[index]
        cur.execute(
            "UPDATE agent_tasks SET updated_at = now() - make_interval(secs => %s) WHERE id = %s",
            (updated_s_ago, tid),
        )
        if reminder_count:
            cur.execute(
                "UPDATE agent_tasks SET reminder_count = %s WHERE id = %s",
                (reminder_count, tid),
            )
    db.commit()
    return tid


def stalled_subtask(db: psycopg.Connection, *, reminder_count: int = 3) -> tuple[int, int, int]:
    """A delegated, stalled subtask: (parent_owner, owner, task_id)."""
    parent_owner = make_agent(db)
    parent = make_task(db, owner=parent_owner, remind_interval_seconds=None)
    owner = make_agent(db)
    tid = make_task(
        db,
        owner=owner,
        parent_id=parent,
        remind_interval_seconds=1800,
        updated_s_ago=7200,
        reminder_count=reminder_count,
    )
    return parent_owner, owner, tid


def inbox_notices(db: psycopg.Connection, agent_id: int) -> list[tuple[str]]:
    db.rollback()
    with db.cursor() as cur:
        cur.execute(
            "SELECT title FROM agent_questions "
            "WHERE agent_id = %s AND resolved_at IS NULL ORDER BY id",
            (agent_id,),
        )
        return cur.fetchall()


def notifications(db: psycopg.Connection, agent_id: int) -> list[tuple[str, str]]:
    db.rollback()
    with db.cursor() as cur:
        cur.execute(
            "SELECT category, subject FROM notification_events WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        return cur.fetchall()


def inbound_messages(db: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str]]:
    """Inbound rows for an agent: (content, kind, source)."""
    db.rollback()  # the daemon committed on its own connection; refresh our view
    with db.cursor() as cur:
        cur.execute(
            "SELECT content, kind, source FROM inbound_messages WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        return cur.fetchall()


def escalated_at(db: psycopg.Connection, tid: int) -> Any:
    """The task's delegator escalation marker (None while unescalated)."""
    db.rollback()  # the daemon committed on its own connection; refresh our view
    with db.cursor() as cur:
        cur.execute("SELECT escalated_at FROM agent_tasks WHERE id = %s", (tid,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


# meiva:note
# Two sweeps inside one stalled window deliver one digest: the first stamps
# `escalated_at`, so the second re-selects the task and skips it. Every 5-min sweep
# used to re-send the digest while the task stayed eligible; the threshold is a
# property of the task, not of one delivery, so it keeps holding across sweeps.
def test_delegator_escalation_at_most_once_per_overdue_window(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    parent_owner, _owner, tid = stalled_subtask(db_conn)
    assert run_escalate(pool, database, event_bus, 3, 0.0) == 1
    assert run_escalate(pool, database, event_bus, 3, 0.0) == 0
    assert len(inbound_messages(db_conn, parent_owner)) == 1
    assert escalated_at(db_conn, tid) is not None


def test_delegator_escalation_retries_after_delivery_failure(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    """A failed digest leaves no marker (no message landed), so the next sweep
    retries; once it lands, later sweeps stay quiet."""
    parent_owner, _owner, tid = stalled_subtask(db_conn)
    with (
        inserts_failing(db_conn, "inbound_messages"),
        pytest.raises(psycopg.errors.RaiseException, match="inbound_messages inserts refused"),
    ):
        run_escalate(pool, database, event_bus, 3, 0.0)
    assert inbound_messages(db_conn, parent_owner) == []
    assert escalated_at(db_conn, tid) is None
    assert run_escalate(pool, database, event_bus, 3, 0.0) == 1  # retried and delivered
    assert len(inbound_messages(db_conn, parent_owner)) == 1
    assert run_escalate(pool, database, event_bus, 3, 0.0) == 0  # marker holds: no third send
    assert len(inbound_messages(db_conn, parent_owner)) == 1


def test_delegator_escalation_rearms_after_owner_update(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    """Any update() clears the marker with the reminder counters; after the
    owner re-engages and a later window re-crosses the threshold, the delegator
    is told again."""
    parent_owner, _owner, tid = stalled_subtask(db_conn)
    assert run_escalate(pool, database, event_bus, 3, 0.0) == 1
    assert run_escalate(pool, database, event_bus, 3, 0.0) == 0
    # The owner updates the task: both update() paths reset the bookkeeping
    # (test_update_resets_reminder_count / the gateway PATCH test below cover
    # the real paths; mirror their write here).
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_tasks SET last_reminded_at = NULL, reminder_count = 0, "
            "escalated_at = NULL WHERE id = %s",
            (tid,),
        )
    db_conn.commit()
    # The window starts over and its reminders re-cross the threshold.
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_tasks SET reminder_count = 3, last_reminded_at = now() WHERE id = %s",
            (tid,),
        )
    db_conn.commit()
    assert run_escalate(pool, database, event_bus, 3, 0.0) == 1
    assert len(inbound_messages(db_conn, parent_owner)) == 2


def test_delegator_escalation_is_not_a_user_notification(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    # meiva:note
    # The delegator leg stays an agent note: telling an agent about a stalled
    # subtask must not ping the user. The real bus is requested because that leg
    # also opens the owner's inbound wake directly.
    # meiva:link base/events/notifications/README.md
    parent_owner, owner, _tid = stalled_subtask(db_conn)

    assert run_escalate(pool, database, event_bus, 3, 0.0) == 1

    assert len(inbound_messages(db_conn, parent_owner)) == 1
    assert notifications(db_conn, owner) == []
    assert notifications(db_conn, parent_owner) == []


# meiva:note
# The copy rule the escalation pass has to keep: its evidence is task bookkeeping
# (reminder_count, and the window a note or a state change restarts), so it may state
# reminders and an unchanged state but may never rule on the owner. An owner that
# answered in chat and one that is gone look identical to this pass, and the same
# words serve both (2026-10-30, task #5862).
# meiva:link meiva_builtins/plugins/meiva_fleet/task_maintenance/docs/task-maintenance/escalation.md
_VERDICT_WORDS = ("unresponsive", "stalled", "not responding")


def assert_states_evidence_not_a_verdict(text: str) -> None:
    lowered = text.lower()
    for verdict in _VERDICT_WORDS:
        assert verdict not in lowered, f"escalation copy rules on the owner: {text!r}"


# meiva:note
# One digest carries a task whose owner is running and one whose owner is
# terminated, under a single header: the pass cannot tell the two apart, so the
# words must not pretend to (the acceptance shape for task #5862).
def test_digest_copy_serves_a_live_and_a_gone_owner_alike(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    delegator = make_agent(db_conn)
    parent = make_task(db_conn, owner=delegator, remind_interval_seconds=None)
    live_owner = make_agent(db_conn)
    gone_owner = make_agent(db_conn, status="terminated")
    live_task = make_task(
        db_conn,
        owner=live_owner,
        parent_id=parent,
        updated_s_ago=7200,
        reminder_count=3,
    )
    gone_task = make_task(
        db_conn,
        owner=gone_owner,
        parent_id=parent,
        updated_s_ago=7200,
        reminder_count=3,
    )

    assert run_escalate(pool, database, event_bus, 3, 0.0) == 1

    messages = inbound_messages(db_conn, delegator)
    assert len(messages) == 1
    content = messages[0][0]
    assert content.splitlines()[0] == (
        "Subtasks still open with no state change "
        "(a note or a change on the task would have restarted the reminders):"
    )
    assert f"- #{live_task} " in content
    assert f"- #{gone_task} " in content
    assert_states_evidence_not_a_verdict(content)


# meiva:note
# The threshold counts cadence periods of silence, not delivered reminders
# (2026-11-07 ruling, task #6354). Reminder delivery is throttled per owner by the
# digest window, so a reminder count stops measuring time as soon as the window is
# wider than the task's interval — three reminders then span many cadences. These
# three tests pin the replacement: silence long enough escalates however few
# reminders went out, recent activity holds it however many did, and a task nobody
# was ever reminded about is not escalated on silence alone.
def _stalled_task(db: psycopg.Connection, *, updated_s_ago: float, reminder_count: int) -> int:
    parent = make_task(db, owner=make_agent(db), remind_interval_seconds=None)
    owner = make_agent(db)
    return make_task(
        db,
        owner=owner,
        parent_id=parent,
        remind_interval_seconds=1800,
        updated_s_ago=updated_s_ago,
        reminder_count=reminder_count,
    )


def test_silence_escalates_after_one_delivered_reminder(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    _stalled_task(db_conn, updated_s_ago=14400, reminder_count=1)
    assert run_escalate(pool, database, event_bus, 3, 0.0) == 1


def test_recent_activity_holds_escalation_despite_many_reminders(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    _stalled_task(db_conn, updated_s_ago=600, reminder_count=10)
    assert run_escalate(pool, database, event_bus, 3, 0.0) == 0


def test_silence_alone_does_not_escalate_a_task_never_reminded(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    _stalled_task(db_conn, updated_s_ago=86400, reminder_count=0)
    assert run_escalate(pool, database, event_bus, 3, 0.0) == 0
