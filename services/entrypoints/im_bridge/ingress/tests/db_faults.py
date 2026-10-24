from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import LiteralString

import psycopg
from psycopg import sql

__all__ = ["failing_insert", "failing_update"]


# meiva:note
# Every INSERT into `table` whose new row satisfies `when` (a SQL condition over `NEW`) raises
# `message` after it wrote its row, until exit. The throwaway Postgres itself fails the
# statement and rolls the transaction back, as a database error would.
@contextmanager
def failing_insert(
    conn: psycopg.Connection,
    table: str,
    *,
    when: LiteralString = "true",
    message: str = "injected insert fault",
) -> Generator[None]:
    function = sql.Identifier(f"fault_raise_{table}")
    trigger = sql.Identifier(f"fault_{table}")
    target = sql.Identifier(table)
    conn.commit()
    conn.execute(
        sql.SQL(
            "CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS "
            "$body$ BEGIN RAISE EXCEPTION {message}; END $body$"
        ).format(function=function, message=sql.Literal(message))
    )
    conn.execute(
        sql.SQL(
            "CREATE TRIGGER {trigger} AFTER INSERT ON {target} "
            "FOR EACH ROW WHEN ({when}) EXECUTE FUNCTION {function}()"
        ).format(trigger=trigger, target=target, function=function, when=sql.SQL(when))
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


# meiva:note
# Every UPDATE of `table` whose row satisfies `when` (a SQL condition over `NEW` and `OLD`)
# raises `message`, until exit; the server aborts the statement and rolls the transaction back.
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
