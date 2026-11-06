from __future__ import annotations

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.config import settings
from base.db import Database
from base.events.live.bus import EventBus
from meiva_builtins.plugins.meiva_fleet.task_maintenance.daemon import run_reminders
from meiva_builtins.plugins.meiva_fleet.task_maintenance.tests.test_task_maintenance_escalation import (
    inbound_messages,
    make_agent,
    make_task,
)

WINDOWS = {"digest_window_p1_seconds": 7200.0, "digest_window_p2_seconds": 10800.0}


@pytest.fixture
def pool(db_url: str):
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield p
    finally:
        p.close()


def _counters(db: psycopg.Connection, task_ids: list[int]) -> list[tuple[int, object, object]]:
    db.rollback()
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, reminder_count, last_reminded_at FROM agent_tasks WHERE id = ANY(%s) "
            "ORDER BY id",
            (task_ids,),
        )
        return cur.fetchall()


# meiva:note
# Three P1 tasks of one owner that came due inside the same window leave as one digest
# carrying all three, and every held-in task still advances its own counter exactly once:
# coalescing must not drop work (task #6354).
def test_one_digest_covers_every_due_p1_task_of_one_owner(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    owner = make_agent(db_conn)
    ids = [
        make_task(
            db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=3600, priority="P1"
        )
        for _ in range(3)
    ]

    assert run_reminders(pool, database, event_bus, 3600.0, **WINDOWS) == 1

    delivered = inbound_messages(db_conn, owner)
    assert len(delivered) == 1
    assert "3 task(s)" in delivered[0][0]
    assert all(f"#{tid} " in delivered[0][0] for tid in ids)
    assert [row[1] for row in _counters(db_conn, ids)] == [1, 1, 1]


# meiva:note
# A P0 task is never held back by the window a P1 task just consumed: its own digest goes
# out in the same sweep, so the priority exists for the reason it was given (task #6354).
def test_p0_owner_gets_its_own_digest_inside_the_window(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    owner = make_agent(db_conn)
    make_task(db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=3600, priority="P1")
    assert run_reminders(pool, database, event_bus, 3600.0, **WINDOWS) == 1

    p0 = make_task(
        db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=3600, priority="P0"
    )

    assert run_reminders(pool, database, event_bus, 3600.0, **WINDOWS) == 1

    delivered = inbound_messages(db_conn, owner)
    assert len(delivered) == 2
    assert f"#{p0} " in delivered[1][0]
    assert "1 task(s)" in delivered[1][0]


# meiva:note
# A task held back by the window is never selected, so its reminder_count stays at zero and
# last_reminded_at stays null: counting happens at delivery. A counter advanced while the
# message waited would escalate a task nobody was ever reminded about (task #6354).
def test_held_back_task_is_not_counted_as_reminded(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    owner = make_agent(db_conn)
    first = make_task(
        db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=3600, priority="P1"
    )
    assert run_reminders(pool, database, event_bus, 3600.0, **WINDOWS) == 1

    second = make_task(
        db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=3600, priority="P1"
    )

    assert run_reminders(pool, database, event_bus, 3600.0, **WINDOWS) == 0

    assert _counters(db_conn, [first])[0][1] == 1
    held = _counters(db_conn, [second])[0]
    assert held[1] == 0
    assert held[2] is None


# meiva:note
# With every window at zero the digest coalescing is off, which is how the daemon behaved
# before the windows existed: callers that pass no windows keep the per-task cadence.
def test_zero_windows_deliver_each_sweep(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    owner = make_agent(db_conn)
    make_task(db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=3600, priority="P1")
    assert run_reminders(pool, database, event_bus, 3600.0) == 1
    make_task(db_conn, owner=owner, remind_interval_seconds=1800, updated_s_ago=3600, priority="P1")
    assert run_reminders(pool, database, event_bus, 3600.0) == 1
