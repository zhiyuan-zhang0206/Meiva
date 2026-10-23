# meiva:note
# A write that fails after its row is inserted, produced by the throwaway Postgres itself.
#
# A route that must roll back everything it wrote when a later write fails is tested against
# the failure of that later write. An `AFTER INSERT` trigger that raises makes the real insert
# fail the way a database error does: the row is written inside the transaction, then the
# statement raises, and Postgres rolls the transaction back.

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import psycopg
from psycopg import sql

__all__ = ["failing_insert"]


# meiva:note
# Every INSERT into `table` raises `message` after it wrote its row, until exit.
@contextmanager
def failing_insert(
    conn: psycopg.Connection, table: str, *, message: str = "injected insert fault"
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
