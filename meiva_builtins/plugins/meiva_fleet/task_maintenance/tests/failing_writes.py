# meiva:note
# Failures the database itself produces for the task-maintenance and task-registry tests: a trigger
# refuses a write while a block runs, so the code under test meets a real error part way through
# its transaction and the test reads back what the rollback left.

from collections.abc import Generator
from contextlib import contextmanager

import psycopg
from psycopg import sql


# meiva:note
# Every INSERT into `table` fails inside the database while the block runs, so a task operation
# that records a row there meets a real failure after its own writes. With `for_agent`, only the
# rows addressed to that agent are refused, so the operation meets the failure part way through a
# batch whose earlier rows were accepted.
@contextmanager
def inserts_failing(
    db: psycopg.Connection, table: str, *, for_agent: int | None = None
) -> Generator[None]:
    refuse = sql.Identifier(f"refuse_{table}")
    only = (
        sql.SQL("")
        if for_agent is None
        else sql.SQL(" WHEN (NEW.agent_id = {agent})").format(agent=sql.Literal(for_agent))
    )
    db.execute(
        sql.SQL(
            "CREATE FUNCTION {refuse}() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN RAISE EXCEPTION {message}; END $$"
        ).format(refuse=refuse, message=sql.Literal(f"{table} inserts refused"))
    )
    db.execute(
        sql.SQL(
            "CREATE TRIGGER {refuse} BEFORE INSERT ON {table} "
            "FOR EACH ROW{only} EXECUTE FUNCTION {refuse}()"
        ).format(refuse=refuse, table=sql.Identifier(table), only=only)
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
