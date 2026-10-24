# meiva:note
# The idempotency claim fails in the database, produced by the throwaway Postgres itself.
#
# A BEFORE INSERT trigger on `api_idempotency` raises the given SQLSTATE for the first `times`
# claims; a sequence counts every attempt outside any transaction, so a rolled-back attempt still
# counts. SQLSTATE 08006 (connection failure) is what psycopg surfaces as an `OperationalError`,
# the failure class a lost connection raises; any other code is a failure that is not one.

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager

import psycopg
from psycopg import sql

__all__ = ["failing_claims"]

_NAME = "claim_outage"


# meiva:note
# The first `times` claim inserts fail with SQLSTATE `errcode`. Yields a reader of how many claim
# inserts were attempted.
@contextmanager
def failing_claims(
    conn: psycopg.Connection, *, errcode: str, times: int
) -> Generator[Callable[[], int]]:
    function = sql.Identifier(_NAME)
    sequence = sql.Identifier(f"{_NAME}_calls")
    conn.commit()
    conn.execute(sql.SQL("CREATE SEQUENCE {}").format(sequence))
    conn.execute(
        sql.SQL(
            "CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $body$ BEGIN "
            "IF nextval({sequence_name}) <= {times} THEN "
            "RAISE EXCEPTION 'claim insert failed by the test' USING ERRCODE = {errcode}; END IF; "
            "RETURN NEW; END $body$"
        ).format(
            function=function,
            sequence_name=sql.Literal(f"{_NAME}_calls"),
            times=sql.Literal(times),
            errcode=sql.Literal(errcode),
        )
    )
    conn.execute(
        sql.SQL(
            "CREATE TRIGGER {function} BEFORE INSERT ON api_idempotency "
            "FOR EACH ROW EXECUTE FUNCTION {function}()"
        ).format(function=function)
    )
    conn.commit()

    def attempts() -> int:
        row = conn.execute(
            sql.SQL("SELECT last_value, is_called FROM {}").format(sequence)
        ).fetchone()
        assert row is not None
        return int(row[0]) if row[1] else 0

    try:
        yield attempts
    finally:
        conn.rollback()
        conn.execute(sql.SQL("DROP TRIGGER {} ON api_idempotency").format(function))
        conn.execute(sql.SQL("DROP FUNCTION {}()").format(function))
        conn.execute(sql.SQL("DROP SEQUENCE {}").format(sequence))
        conn.commit()
