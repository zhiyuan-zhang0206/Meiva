from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from meiva.tests.fixtures.pin_agent import pin_agent
from meiva_builtins.plugins.meiva_fleet import task_registry
from meiva_builtins.plugins.meiva_fleet.task_maintenance.tests.failing_writes import inserts_failing

# meiva:note
# These tests drive the fleet task registry, whose board refreshes publish through the SDK's event
# bus; it needs the generator the SDK root carries, so each runs under the bare installation
# `sdk_root` holds.
pytestmark = pytest.mark.usefixtures("sdk_root")


def _seed_agent(db: psycopg.Connection) -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        aid = int(row[0])
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running')", (aid,)
        )
    db.commit()
    return aid


@pytest.fixture
def agent_id(db_conn: psycopg.Connection) -> Iterator[int]:
    aid = _seed_agent(db_conn)
    pin_agent(aid)
    yield aid


@pytest.fixture
def root_task_id(db_conn: psycopg.Connection) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, status, created_by, is_root) "
            "VALUES ('Root', 'root', 'in_progress', 'system', TRUE) RETURNING id"
        )
        row = cur.fetchone()
        assert row is not None
        rid = int(row[0])
    db_conn.commit()
    return rid


def _audit(db: psycopg.Connection, agent_id: int, name: str) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT attributes FROM audit_events WHERE agent_id=%s AND event_name=%s ORDER BY id",
        (agent_id, name),
    ).fetchall()
    db.commit()
    return [row[0] for row in rows]


def test_a_created_task_is_recorded_with_its_row(
    redis_url: str, db_conn: psycopg.Connection, agent_id: int, root_task_id: int
) -> None:
    task = task_registry.create(
        "audited", "detail", parent=root_task_id, operation_key=str(uuid4())
    )

    [recorded] = _audit(db_conn, agent_id, "task_create")
    assert (recorded["task_id"], recorded["title"]) == (task.id, "audited")


def test_a_task_whose_audit_fact_cannot_be_recorded_is_not_created(
    db_conn: psycopg.Connection,
    agent_id: int,
    root_task_id: int,
) -> None:
    with (
        inserts_failing(db_conn, "audit_events"),
        pytest.raises(psycopg.errors.RaiseException, match="audit_events inserts refused"),
    ):
        task_registry.create("never", "detail", parent=root_task_id, operation_key=str(uuid4()))

    count = db_conn.execute("SELECT count(*) FROM agent_tasks WHERE title='never'").fetchone()
    db_conn.commit()
    assert count == (0,)


def test_a_task_update_is_recorded_with_its_change(
    redis_url: str, db_conn: psycopg.Connection, agent_id: int, root_task_id: int
) -> None:
    task = task_registry.create("title", "detail", parent=root_task_id, operation_key=str(uuid4()))

    task_registry.update(task.id, remind_interval_seconds=1800, operation_key=str(uuid4()))

    [recorded] = _audit(db_conn, agent_id, "task_update")
    assert recorded["task_id"] == task.id
