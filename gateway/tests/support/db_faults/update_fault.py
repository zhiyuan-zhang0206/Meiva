# meiva:note
# A write that fails when it updates a row, produced by the throwaway Postgres itself.
#
# A route that publishes durable files and then commits a receipt is tested against the failure
# of that commit: a `BEFORE UPDATE` trigger raises, so the server aborts the statement and rolls
# the surrounding transaction back, with the files already on disk. Only the rows that match
# `when` (a SQL condition over `NEW` and `OLD`) are refused, so a claim that only inserts is
# untouched.

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import LiteralString

import psycopg
from psycopg import sql

__all__ = ["failing_update"]


# meiva:note
# Every UPDATE of `table` whose row satisfies `when` raises `message`, until exit.
@contextmanager
def failing_update(
    conn: psycopg.Connection,
    table: str,
    *,
    when: LiteralString = "true",
    message: str = "injected update fault",
) -> Generator[None]:
    function = sql.Identifier(f"fault_raise_update_{table}")
    trigger = sql.Identifier(f"fault_update_{table}")
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
            "CREATE TRIGGER {trigger} BEFORE UPDATE ON {target} "
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
