# meiva:note
# A failing write, made in the database itself.
#
# A trigger that raises is a real failure of the statement: the driver reports a database error,
# the transaction rolls back, and the code under test meets exactly what it meets when a write
# fails in production. The trigger lives on the test's throwaway database and is dropped when the
# context ends.

from collections.abc import Generator
from contextlib import contextmanager
from typing import LiteralString

import psycopg

__all__ = ["failing_write"]


# meiva:note
# Make `events` ("INSERT", "UPDATE", "INSERT OR UPDATE") on `table` rows matching the SQL
# condition `when` (over `NEW`) fail with a database error.
@contextmanager
def failing_write(
    conn: psycopg.Connection,
    *,
    table: LiteralString,
    events: LiteralString,
    when: LiteralString = "true",
) -> Generator[None]:
    conn.execute(
        "CREATE OR REPLACE FUNCTION injected_fault() RETURNS trigger LANGUAGE plpgsql AS "
        "$$ BEGIN RAISE EXCEPTION 'injected fault'; END $$"
    )
    conn.execute(
        f"CREATE TRIGGER injected_fault BEFORE {events} ON {table} "
        f"FOR EACH ROW WHEN ({when}) EXECUTE FUNCTION injected_fault()"
    )
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        conn.execute(f"DROP TRIGGER IF EXISTS injected_fault ON {table}")
        conn.execute("DROP FUNCTION IF EXISTS injected_fault()")
        conn.commit()
