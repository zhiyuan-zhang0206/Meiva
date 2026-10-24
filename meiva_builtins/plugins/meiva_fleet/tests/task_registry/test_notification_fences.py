from uuid import uuid4

import psycopg

from base.agents.tasks.delivery import lock_task_note_owner
from base.db import pool as db_pool
from base.db.code_version_gate import ProcessDbGate
from meiva.tests.fixtures.pin_agent import pin_agent
from meiva_builtins.plugins.meiva_fleet import task_registry
from meiva_builtins.plugins.meiva_fleet.tests.task_registry.seeds import (
    root_task_id as root_task_id,
)
from meiva_builtins.plugins.meiva_fleet.tests.task_registry.seeds import seed_agent
from services.wake.delivery_watchdog.dead_letter import dead_letter_stale_pending_terminated


def test_reassigned_task_refuses_delayed_home_wake(
    redis_url: str,
    db_conn: psycopg.Connection,
    root_task_id: int,
) -> None:
    actor = seed_agent(db_conn)
    pin_agent(actor)
    owner = seed_agent(db_conn, status="terminated")
    next_owner = seed_agent(db_conn)
    task = task_registry.create(
        "fenced", "work", parent=root_task_id, owner=owner, operation_key=str(uuid4())
    )
    selected = db_conn.execute(
        "SELECT agent_id,id FROM inbound_messages WHERE agent_id=%s AND status='pending'", (owner,)
    ).fetchall()
    assert len(selected) == 1
    with db_conn.cursor() as cur:
        assert lock_task_note_owner(cur, owner, selected[0][1]) is True
    db_conn.execute("UPDATE agent_tasks SET owner=%s WHERE id=%s", (next_owner, task.id))
    db_conn.commit()
    with db_conn.cursor() as cur:
        assert lock_task_note_owner(cur, owner, selected[0][1]) is False
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='resurrect'", (owner,)
    ).fetchone() == (0,)


def test_assignment_deadline_has_inspectable_failure(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int, *, database_gate: ProcessDbGate
) -> None:
    actor = seed_agent(db_conn)
    pin_agent(actor)
    owner = seed_agent(db_conn, status="terminated")
    task_registry.create(
        "expires", "work", parent=root_task_id, owner=owner, operation_key=str(uuid4())
    )
    db_conn.execute(
        "UPDATE inbound_messages SET created_at=now()-interval '25 hours' WHERE agent_id=%s",
        (owner,),
    )
    db_conn.commit()
    with db_pool(gate=database_gate) as note_pool:
        assert note_pool is not None
        assert dead_letter_stale_pending_terminated(note_pool, 86400) == 1
    row = db_conn.execute(
        "SELECT status,payload FROM inbound_messages WHERE agent_id=%s", (owner,)
    ).fetchone()
    assert row is not None and row[0] == "done"
    assert row[1]["delivery_result"] == {
        "outcome": "failed",
        "reason": "resurrection_deadline_expired",
    }
