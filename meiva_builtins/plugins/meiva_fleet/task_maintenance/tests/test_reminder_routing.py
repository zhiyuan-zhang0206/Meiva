from __future__ import annotations

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.config import settings
from base.db import Database
from base.events.live.bus import EventBus
from meiva_builtins.plugins.meiva_fleet.task_maintenance import routing
from meiva_builtins.plugins.meiva_fleet.task_maintenance.daemon import (
    run_escalate,
    run_reminders,
)
from meiva_builtins.plugins.meiva_fleet.task_maintenance.tests.test_task_maintenance_escalation import (
    escalated_at,
    inbound_messages,
    inbox_notices,
    make_agent,
    make_task,
)


@pytest.fixture
def pool(db_url: str):
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield p
    finally:
        p.close()


# meiva:note
# A terminated owner with one due task under a task the manager owns. The reminder,
# reroute and escalation tests below all start from this shape: the dead agent holds the
# work, a live agent owns the parent task that delegated it.
def _dead_owner_under(
    db: psycopg.Connection, manager: int, *, reminder_count: int = 0
) -> tuple[int, int]:
    parent = make_task(db, owner=manager, remind_interval_seconds=None)
    dead = make_agent(db, status="terminated")
    return dead, make_task(
        db, owner=dead, parent_id=parent, updated_s_ago=3600, reminder_count=reminder_count
    )


# meiva:note
# The dead owner's queue stays empty and the manager receives the digest: a terminated
# agent cannot act on a reminder, and a queue nobody reads is where the 2026-10-25
# incident's reminders sat. The reroute also marks the task `escalated_at`, so the
# escalate pass does not repeat at its threshold what the manager was just told.
def test_terminated_owner_digest_goes_to_the_manager(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    manager = make_agent(db_conn)
    dead, tid = _dead_owner_under(db_conn, manager)

    assert run_reminders(pool, database, event_bus, 3600.0) == 1

    assert inbound_messages(db_conn, dead) == []
    delivered = inbound_messages(db_conn, manager)
    assert len(delivered) == 1
    content, kind, source = delivered[0]
    assert (kind, source) == ("system_note", "system")
    assert f"Agent #{dead} (terminated)" in content
    assert f"#{tid} " in content
    assert escalated_at(db_conn, tid) is not None


# meiva:note
# 65 due tasks of one dead owner are one message, not 65 (the aggregation the 2026-10-25
# ruling asked for).
def test_one_digest_covers_every_due_task_of_the_dead_owner(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    manager = make_agent(db_conn)
    parent = make_task(db_conn, owner=manager, remind_interval_seconds=None)
    dead = make_agent(db_conn, status="terminated")
    task_ids = [
        make_task(db_conn, owner=dead, parent_id=parent, updated_s_ago=3600) for _ in range(3)
    ]

    assert run_reminders(pool, database, event_bus, 3600.0) == 1

    delivered = inbound_messages(db_conn, manager)
    assert len(delivered) == 1
    content = delivered[0][0]
    assert "3 task(s) due under 1 terminated owner(s)" in content
    assert all(f"#{task_id} " in content for task_id in task_ids)


# meiva:note
# The manager is the recipient, so two dead owners under it are one message, not one per
# owner.
def test_dead_owners_under_one_manager_share_a_digest(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    manager = make_agent(db_conn)
    parent = make_task(db_conn, owner=manager, remind_interval_seconds=None)
    dead_owners = [make_agent(db_conn, status="terminated") for _ in range(2)]
    for dead in dead_owners:
        make_task(db_conn, owner=dead, parent_id=parent, updated_s_ago=3600)

    assert run_reminders(pool, database, event_bus, 3600.0) == 1

    delivered = inbound_messages(db_conn, manager)
    assert len(delivered) == 1
    content = delivered[0][0]
    assert "2 task(s) due under 2 terminated owner(s)" in content
    for dead in dead_owners:
        assert f"Agent #{dead} (terminated)" in content
        assert inbound_messages(db_conn, dead) == []


# meiva:note
# The dead owner's own container task names itself, not a manager: the walk has to
# continue past it to the first ancestor whose owner is still alive.
def test_manager_walks_past_a_container_the_dead_agent_owns(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    manager = make_agent(db_conn)
    domain = make_task(db_conn, owner=manager, remind_interval_seconds=None)
    dead = make_agent(db_conn, status="terminated")
    container = make_task(db_conn, owner=dead, parent_id=domain, remind_interval_seconds=None)
    tid = make_task(db_conn, owner=dead, parent_id=container, updated_s_ago=3600)

    assert run_reminders(pool, database, event_bus, 3600.0) == 1

    assert inbound_messages(db_conn, dead) == []
    delivered = inbound_messages(db_conn, manager)
    assert len(delivered) == 1 and f"#{tid} " in delivered[0][0]


# meiva:note
# A top-level task of a dead owner has no live owner above it, so the human queue is the
# only recipient left; the notice is a question (require_response) and carries no
# `escalated_at`, because no manager was told.
def test_no_live_manager_notifies_the_human_queue(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    root = make_task(db_conn, owner=None, remind_interval_seconds=None)
    dead = make_agent(db_conn, status="terminated")
    tid = make_task(db_conn, owner=dead, parent_id=root, updated_s_ago=3600)

    assert run_reminders(pool, database, event_bus, 3600.0) == 1

    assert inbound_messages(db_conn, dead) == []
    notices = inbox_notices(db_conn, dead)
    assert len(notices) == 1
    title, require_response = notices[0]
    assert require_response is True
    assert f"Agent #{dead} is terminated" in title
    assert escalated_at(db_conn, tid) is None


def test_rerouted_reminder_does_not_repeat_at_the_escalation_threshold(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    manager = make_agent(db_conn)
    _dead, _tid = _dead_owner_under(db_conn, manager, reminder_count=2)

    assert run_reminders(pool, database, event_bus, 3600.0) == 1
    assert run_escalate(pool, database, event_bus, 3) == 0
    assert len(inbound_messages(db_conn, manager)) == 1


# meiva:note
# The escalate pass addresses the same manager: a delegator terminated after delegating
# cannot catch the stall, so the walk continues to the nearest live owner above it.
def test_delegator_escalation_skips_a_terminated_parent_owner(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    manager = make_agent(db_conn)
    domain = make_task(db_conn, owner=manager, remind_interval_seconds=None)
    dead = make_agent(db_conn, status="terminated")
    parent = make_task(db_conn, owner=dead, parent_id=domain, remind_interval_seconds=None)
    owner = make_agent(db_conn)
    make_task(db_conn, owner=owner, parent_id=parent, updated_s_ago=7200, reminder_count=3)

    assert run_escalate(pool, database, event_bus, 3) == 1

    assert inbound_messages(db_conn, dead) == []
    assert len(inbound_messages(db_conn, manager)) == 1


def test_delegator_escalation_falls_back_to_the_human_queue(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    dead = make_agent(db_conn, status="terminated")
    parent = make_task(db_conn, owner=dead, remind_interval_seconds=None)
    owner = make_agent(db_conn)
    make_task(db_conn, owner=owner, parent_id=parent, updated_s_ago=7200, reminder_count=3)

    assert run_escalate(pool, database, event_bus, 3) == 1

    assert inbound_messages(db_conn, dead) == []
    assert [title for title, _ in inbox_notices(db_conn, owner)]


# meiva:note
# A corrupt parent chain must end the walk instead of looping forever: the cycle guard is
# the visited set inside `nearest_manager`, not a depth limit that would need a
# justification nobody can write.
def test_nearest_manager_stops_on_a_parent_cycle(db_conn: psycopg.Connection) -> None:
    dead = make_agent(db_conn, status="terminated")
    first = make_task(db_conn, owner=dead, remind_interval_seconds=None)
    second = make_task(db_conn, owner=dead, parent_id=first, remind_interval_seconds=None)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agent_tasks SET parent_id = %s WHERE id = %s", (second, first))
    db_conn.commit()

    with db_conn.cursor() as cur:
        assert routing.nearest_manager(cur, [first, second]) == {first: None, second: None}
