"""Delegator escalation marker (`escalated_at`) — the marker's whole lifecycle.

The delegation leg of the task-maintenance daemon reports an unresponsive owner
by delivering one digest to the delegator. `escalated_at`, stamped in the same
transaction as the digest's inbound insert, makes that at-most-once per overdue
window: a failed delivery leaves it unset (the next sweep retries), and both
update() paths clear it with the reminder counters (re-arming the task's next
window). The daemon's reminder/escalation behaviour suite lives in
test_task_maintenance_daemon.py; the marker tests keep the direct delivery real
because the marker write lives inside it, and the gateway leg exercises the real
HTTP boundary — kept together so the lifecycle reads in one place.
"""

from __future__ import annotations

import json
from itertools import count
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.config import settings
from base.db import Database
from base.events.live.bus import EventBus, event_bus_config_from_settings
from meiva_builtins.plugins.meiva_fleet.task_maintenance import escalation
from meiva_builtins.plugins.meiva_fleet.task_maintenance.daemon import run_escalate

_TASK_TITLE = count(1)


@pytest.fixture
def pool(db_url: str):
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield p
    finally:
        p.close()


def make_agent(db: psycopg.Connection) -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        aid = int(cur.fetchone()[0])  # type: ignore[index]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running')",
            (aid,),
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
) -> int:
    # Distinct titles by default: the agent_tasks partial unique index forbids
    # two in_progress rows sharing a title, and these tests create many.
    if title is None:
        title = f"t-{next(_TASK_TITLE)}"
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, status, owner, created_by, "
            "parent_id, remind_interval_seconds) "
            "VALUES (%s, 'd', 'in_progress', %s, 'user', %s, %s) RETURNING id",
            (title, owner, parent_id, remind_interval_seconds),
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


# meiva:note
# A stalled top-level task: (owner, task_id, parent_id). Its parent has no owner,
# which is the real shape of the user leg — a top-level task descends from the
# ownerless system root, so no delegating agent exists to catch the stall and
# the user is told instead.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def human_leg_subtask(db: psycopg.Connection, *, reminder_count: int = 3) -> tuple[int, int, int]:
    parent = make_task(db, owner=None, remind_interval_seconds=None)
    owner = make_agent(db)
    tid = make_task(
        db,
        owner=owner,
        parent_id=parent,
        remind_interval_seconds=1800,
        updated_s_ago=7200,
        reminder_count=reminder_count,
    )
    return owner, tid, parent


def inbox_notices(db: psycopg.Connection, agent_id: int) -> list[tuple[str, bool]]:
    db.rollback()
    with db.cursor() as cur:
        cur.execute(
            "SELECT title, require_response FROM agent_notices "
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


def test_delegator_escalation_at_most_once_per_overdue_window(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    """Two sweeps inside one stalled window deliver one digest: the first
    stamps `escalated_at`, so the second re-selects the task and skips it.
    (Defect class fixed here: every 5-min sweep re-sent the digest while
    reminder_count sat at the threshold — reminder delivery is backoff-gated
    for up to 24h.)"""
    parent_owner, _owner, tid = stalled_subtask(db_conn)
    assert run_escalate(pool, database, event_bus, 3) == 1
    assert run_escalate(pool, database, event_bus, 3) == 0
    assert len(inbound_messages(db_conn, parent_owner)) == 1
    assert escalated_at(db_conn, tid) is not None


def test_delegator_escalation_retries_after_delivery_failure(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    """A failed digest leaves no marker (no message landed), so the next sweep
    retries; once it lands, later sweeps stay quiet."""
    parent_owner, _owner, tid = stalled_subtask(db_conn)
    real = escalation.insert_inbound_message_in_transaction

    def unavailable(*args: object, **kwargs: object) -> None:
        raise psycopg.OperationalError("queue unavailable")

    monkeypatch.setattr(escalation, "insert_inbound_message_in_transaction", unavailable)
    with pytest.raises(psycopg.OperationalError):
        run_escalate(pool, database, event_bus, 3)
    assert inbound_messages(db_conn, parent_owner) == []
    assert escalated_at(db_conn, tid) is None
    monkeypatch.setattr(escalation, "insert_inbound_message_in_transaction", real)
    assert run_escalate(pool, database, event_bus, 3) == 1  # retried and delivered
    assert len(inbound_messages(db_conn, parent_owner)) == 1
    assert run_escalate(pool, database, event_bus, 3) == 0  # marker holds: no third send
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
    assert run_escalate(pool, database, event_bus, 3) == 1
    assert run_escalate(pool, database, event_bus, 3) == 0
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
    assert run_escalate(pool, database, event_bus, 3) == 1
    assert len(inbound_messages(db_conn, parent_owner)) == 2


# meiva:note
# An `EventBus` whose publication is observed instead of sent. `drop` is the
# Redis outage the transport already models: `publish_best_effort_sync` reports
# the publication as failed by returning None. The producer must neither record
# that as delivered nor lose the notice.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
class _RecordingBus(EventBus):
    def __init__(self) -> None:
        super().__init__(event_bus_config_from_settings())
        self.events: list[dict[str, Any]] = []
        self.drop = False

    def publish_best_effort_sync(
        self,
        payload: str,
        *,
        channel: str | None = None,
        decode_responses: bool = True,
        context: str = "",
    ) -> int | None:
        del channel, decode_responses, context
        if self.drop:
            return None
        self.events.append(json.loads(payload))
        return 1

    def notifications(self) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("role") == "notification"]


def test_user_escalation_delivers_a_needs_input_notification(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
) -> None:
    # meiva:note
    # A stalled top-level task escalates to the user through the notification
    # spine, not straight into the inbox: one `core.needs_input` delivery row and
    # one live `notification` event, next to the inbox notice that was already
    # there. The defect: the escalation wrote `agent_notices` and published only
    # an `agent_updated` console refresh, so it never reached a phone or the Mac.
    # meiva:link base/events/notifications/docs/notifications.meiva.okf.md
    owner, tid, _parent = human_leg_subtask(db_conn)
    live = _RecordingBus()

    assert run_escalate(pool, database, live, 3) == 1

    notices = inbox_notices(db_conn, owner)
    assert [require_response for _title, require_response in notices] == [True]
    title = notices[0][0]
    assert f"#{tid}" in title
    assert notifications(db_conn, owner) == [("core.needs_input", title)]
    delivered = live.notifications()
    assert len(delivered) == 1
    assert delivered[0]["category"] == "core.needs_input"
    assert delivered[0]["subject"] == title
    assert delivered[0]["agent_id"] == owner


def test_user_escalation_survives_a_lost_announce(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
) -> None:
    # meiva:note
    # A Redis outage costs the live push, never the escalation: the inbox notice
    # and the delivery row are committed before anything is announced.
    # meiva:link base/events/notifications/docs/notifications.meiva.okf.md
    owner, _tid, _parent = human_leg_subtask(db_conn)
    live = _RecordingBus()
    live.drop = True

    assert run_escalate(pool, database, live, 3) == 1

    assert len(inbox_notices(db_conn, owner)) == 1
    assert [category for category, _subject in notifications(db_conn, owner)] == [
        "core.needs_input"
    ]
    assert live.notifications() == []


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
    # meiva:link base/events/notifications/docs/notifications.meiva.okf.md
    parent_owner, owner, _tid = stalled_subtask(db_conn)

    assert run_escalate(pool, database, event_bus, 3) == 1

    assert len(inbound_messages(db_conn, parent_owner)) == 1
    assert notifications(db_conn, owner) == []
    assert notifications(db_conn, parent_owner) == []
