# meiva:note
# Faults made in the database itself, for the tests of what a transaction rolls back.
#
# A write that must fail at a chosen step fails there for real: a trigger on the table raises, so
# the server aborts the statement and the surrounding transaction exactly as a full disk or a
# failed constraint would. The trigger exists only for the block and is dropped on the way out,
# whatever happened inside.

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import psycopg
from psycopg import sql

__all__ = ["refusing_inserts"]


# meiva:note
# Every INSERT into `table` raises in the database while the block runs.
@contextmanager
def refusing_inserts(db_url: str, table: str) -> Generator[None]:
    name = sql.Identifier(f"refuse_insert_{table}")
    target = sql.Identifier(table)
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute(
            sql.SQL(
                "CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS "
                "$$ BEGIN RAISE EXCEPTION 'insert refused by the test'; END $$"
            ).format(name)
        )
        conn.execute(
            sql.SQL(
                "CREATE TRIGGER {} BEFORE INSERT ON {} FOR EACH ROW EXECUTE FUNCTION {}()"
            ).format(name, target, name)
        )
        try:
            yield
        finally:
            conn.execute(sql.SQL("DROP TRIGGER {} ON {}").format(name, target))
            conn.execute(sql.SQL("DROP FUNCTION {}()").format(name))
