"""Shared seeding for the task-registry tests: an agent row, a no-op system note, the root task."""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

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


# meiva:note
# Every INSERT into `table` fails inside the database while the block runs, so a task operation
# that records a row there meets a real failure after its own writes.
@contextmanager
def inserts_failing(db: psycopg.Connection, table: str) -> Generator[None]:
    refuse = sql.Identifier(f"refuse_{table}")
    db.execute(
        sql.SQL(
            "CREATE FUNCTION {refuse}() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN RAISE EXCEPTION {message}; END $$"
        ).format(refuse=refuse, message=sql.Literal(f"{table} inserts refused"))
    )
    db.execute(
        sql.SQL(
            "CREATE TRIGGER {refuse} BEFORE INSERT ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION {refuse}()"
        ).format(refuse=refuse, table=sql.Identifier(table))
    )
    db.commit()
    try:
        yield
    finally:
        db.rollback()
        db.execute(
            sql.SQL("DROP TRIGGER {refuse} ON {table}").format(
                refuse=refuse, table=sql.Identifier(table)
            )
        )
        db.execute(sql.SQL("DROP FUNCTION {refuse}()").format(refuse=refuse))
        db.commit()


# meiva:note
# Advancing the reminder counters of one task fails inside the database while the block runs, so a
# reminder pass that has already written other rows meets a real failure part way through.
@contextmanager
def reminder_counter_failing(db: psycopg.Connection, task_id: int) -> Generator[None]:
    db.execute(
        "CREATE FUNCTION refuse_counter() RETURNS trigger LANGUAGE plpgsql AS "
        "$$ BEGIN RAISE EXCEPTION 'reminder counter refused'; END $$"
    )
    db.execute(
        sql.SQL(
            "CREATE TRIGGER refuse_counter BEFORE UPDATE ON agent_tasks FOR EACH ROW "
            "WHEN (OLD.id = {task} AND NEW.reminder_count > OLD.reminder_count) "
            "EXECUTE FUNCTION refuse_counter()"
        ).format(task=sql.Literal(task_id))
    )
    db.commit()
    try:
        yield
    finally:
        db.rollback()
        db.execute("DROP TRIGGER refuse_counter ON agent_tasks")
        db.execute("DROP FUNCTION refuse_counter()")
        db.commit()


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
