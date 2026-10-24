"""Shared seeding for the task-registry tests: an agent row, a no-op system note, the root task."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest

from meiva.tests.fixtures.pin_agent import pin_agent
from meiva_builtins.plugins.meiva_fleet import task_registry


def seed_agent(db: psycopg.Connection, *, status: str = "running", spawner: str = "test") -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        aid = cur.fetchone()[0]  # type: ignore[index]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, %s, %s)",
            (aid, spawner, status),  # pyright: ignore[reportUnknownArgumentType]
        )
    db.commit()
    return aid


@pytest.fixture(autouse=True)
def root_task_id(db_conn: psycopg.Connection) -> Iterator[int]:
    """Seed the system root task (is_root=TRUE, unowned) before each test and
    yield its id. create() requires an explicit parent, so tests anchor their
    tasks under this root (parent=root_task_id); the root itself stays
    parentless."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, status, created_by, is_root) "
            "VALUES ('Root', 'root', 'in_progress', 'system', TRUE) RETURNING id"
        )
        rid = cur.fetchone()[0]  # type: ignore[index]
    db_conn.commit()
    yield rid


def persisted_priority(db: psycopg.Connection, task_id: int) -> str:
    with db.cursor() as cur:
        cur.execute("SELECT priority FROM agent_tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def parent_of(db: psycopg.Connection, task_id: int) -> int | None:
    with db.cursor() as cur:
        cur.execute("SELECT parent_id FROM agent_tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def persisted_parent(db: psycopg.Connection, task_id: int) -> int | None:
    with db.cursor() as cur:
        cur.execute("SELECT parent_id FROM agent_tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def seed_owned_task(db: psycopg.Connection, root: int) -> tuple[int, int, int]:
    actor, owner = seed_agent(db), seed_agent(db)
    pin_agent(actor)
    task = task_registry.create(
        "receipt task", "work", parent=root, owner=owner, operation_key=str(uuid4())
    )
    return actor, owner, task.id
