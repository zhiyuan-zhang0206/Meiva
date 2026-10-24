from collections.abc import Generator
from contextlib import contextmanager
from typing import LiteralString

import psycopg
from psycopg import sql


# meiva:note
# A table fault the real database can produce: a trigger that refuses the write under test. The
# throwaway database is cleaned of rows between tests, not of functions and triggers, so the
# fault is dropped when the test is done.
@contextmanager
def refused_write(
    conn: psycopg.Connection,
    *,
    table: LiteralString,
    event: LiteralString,
    when: sql.Composable,
) -> Generator[None]:
    conn.execute(
        "CREATE FUNCTION refuse_write() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "RAISE EXCEPTION 'write refused'; END $$"
    )
    conn.execute(
        sql.SQL(
            "CREATE TRIGGER refuse_write BEFORE {event} ON {table} "
            "FOR EACH ROW WHEN ({when}) EXECUTE FUNCTION refuse_write()"
        ).format(event=sql.SQL(event), table=sql.Identifier(table), when=when)
    )
    conn.commit()
    try:
        yield
    finally:
        conn.execute(
            sql.SQL("DROP TRIGGER refuse_write ON {table}; DROP FUNCTION refuse_write()").format(
                table=sql.Identifier(table)
            )
        )
        conn.commit()
