# meiva:note
# A write that fails when a row is written, produced by the throwaway Postgres itself.
#
# An op that publishes files or sends a batch is tested against the failure of one of its
# writes: a `BEFORE INSERT OR UPDATE` trigger raises for the rows that match `when` (a SQL
# condition over `NEW`), so the server aborts the statement and rolls the surrounding
# transaction back exactly as a database error would. Rows that do not match are untouched.

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import LiteralString

import psycopg
from psycopg import sql

__all__ = ["failing_write"]


# meiva:note
# Every INSERT or UPDATE of a row of `table` that satisfies `when` raises `message`, until exit.
@contextmanager
def failing_write(
    conn: psycopg.Connection,
    table: str,
    *,
    when: LiteralString,
    message: str,
) -> Generator[None]:
    function = sql.Identifier(f"fault_raise_write_{table}")
    trigger = sql.Identifier(f"fault_write_{table}")
    target = sql.Identifier(table)
    conn.commit()
    conn.execute(
        sql.SQL(
            "CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS "
            "$body$ BEGIN IF {when} THEN RAISE EXCEPTION {message}; END IF; RETURN NEW; END $body$"
        ).format(function=function, when=sql.SQL(when), message=sql.Literal(message))
    )
    conn.execute(
        sql.SQL(
            "CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE ON {target} "
            "FOR EACH ROW EXECUTE FUNCTION {function}()"
        ).format(trigger=trigger, target=target, function=function)
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
