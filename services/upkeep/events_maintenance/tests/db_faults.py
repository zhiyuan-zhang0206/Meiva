# meiva:note
# Faults produced by the throwaway Postgres itself.
#
# `table_missing` renames a table away and commits the rename, so every statement that names it
# raises `UndefinedTable` on any connection. `table_locked` holds an exclusive lock on a table from
# a second connection, so every statement that reads it blocks until the block ends. Both undo
# themselves when the block ends.
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Literal

import psycopg
from psycopg import sql


@contextmanager
def table_missing(conn: psycopg.Connection, table: str) -> Generator[None]:
    moved = sql.Identifier(f"{table}_moved")
    conn.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(sql.Identifier(table), moved))
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        conn.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(moved, sql.Identifier(table)))
        conn.commit()


@contextmanager
def table_locked(db_url: str, table: str) -> Generator[None]:
    with psycopg.connect(db_url) as holder:
        holder.execute(
            sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(sql.Identifier(table))
        )
        try:
            yield
        finally:
            holder.rollback()


# meiva:note
# Every `on` statement (INSERT or UPDATE) against `table` raises after it wrote its row, until the
# block ends: an `AFTER` trigger that raises makes the real statement fail the way a database error
# does, and Postgres rolls the transaction back. The trigger is committed so that any connection
# meets it.
@contextmanager
def write_fails(
    conn: psycopg.Connection, table: str, *, on: Literal["INSERT", "UPDATE"]
) -> Generator[None]:
    function = sql.Identifier(f"fault_raise_{table}")
    trigger = sql.Identifier(f"fault_{table}")
    target = sql.Identifier(table)
    conn.commit()
    conn.execute(
        sql.SQL(
            "CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS "
            "$body$ BEGIN RAISE EXCEPTION 'injected insert fault'; END $body$"
        ).format(function=function)
    )
    conn.execute(
        sql.SQL(
            "CREATE TRIGGER {trigger} AFTER {event} ON {target} "
            "FOR EACH ROW EXECUTE FUNCTION {function}()"
        ).format(trigger=trigger, event=sql.SQL(on), target=target, function=function)
    )
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        conn.execute(
            sql.SQL("DROP TRIGGER {trigger} ON {target}").format(trigger=trigger, target=target)
        )
        conn.execute(sql.SQL("DROP FUNCTION {function}()").format(function=function))
        conn.commit()
