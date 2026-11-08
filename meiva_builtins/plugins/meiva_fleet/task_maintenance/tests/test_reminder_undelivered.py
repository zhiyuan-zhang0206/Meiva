from __future__ import annotations

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.config import settings
from base.db import Database
from base.events.live.bus import EventBus
from base.telemetry.tests.capture.event_capture import EmittedEvents
from meiva_builtins.plugins.meiva_fleet.task_maintenance.daemon import run_reminders
from meiva_builtins.plugins.meiva_fleet.task_maintenance.tests.test_task_maintenance_escalation import (
    make_agent,
    make_task,
)
from meiva_builtins.plugins.meiva_fleet.task_maintenance.undelivered import (
    report_undelivered,
)

GRACE = 600.0


@pytest.fixture
def pool(db_url: str):
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield p
    finally:
        p.close()


# meiva:note
# One task that stayed overdue well past the grace with `reminder_count` still zero has never
# been delivered; the count is what makes it visible, because no message exists to notice
# (task #6399).
def test_an_overdue_task_that_never_got_a_reminder_is_reported(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    emitted_events: EmittedEvents,
) -> None:
    owner = make_agent(db_conn)
    make_task(db_conn, owner=owner, remind_interval_seconds=60, updated_s_ago=4000)

    report_undelivered(pool, GRACE)

    assert emitted_events.attributes("task_reminder_undelivered") == [
        {"owner_id": owner, "count": 1}
    ]


# meiva:note
# The negative control that keeps the number readable: a task still inside its grace is on the
# normal path -- due, not yet selected -- so the count stays zero and the event says nothing.
def test_a_task_inside_the_grace_is_not_reported(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    emitted_events: EmittedEvents,
) -> None:
    owner = make_agent(db_conn)
    make_task(db_conn, owner=owner, remind_interval_seconds=60, updated_s_ago=60 + GRACE - 30)

    report_undelivered(pool, GRACE)

    assert emitted_events.names() == []


# meiva:note
# A working delivery path must never produce the event: the sweep delivers the reminder and
# advances the counter in the same transaction, so the same task that was reportable a moment
# ago is silent once it is actually reminded (task #6399).
def test_a_delivered_reminder_leaves_nothing_to_report(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    emitted_events: EmittedEvents,
) -> None:
    owner = make_agent(db_conn)
    make_task(db_conn, owner=owner, remind_interval_seconds=60, updated_s_ago=4000)

    assert run_reminders(pool, database, event_bus, 3600.0, undelivered_grace_seconds=GRACE) == 1

    report_undelivered(pool, GRACE)

    assert emitted_events.attributes("task_reminder_undelivered") == []
