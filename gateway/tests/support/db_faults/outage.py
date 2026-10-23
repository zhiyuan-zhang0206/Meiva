# meiva:note
# A write that fails for a while and then works, produced by the throwaway Postgres itself.
#
# A writer that must ride out a transient database outage is tested against the failure class
# a lost connection raises. A `BEFORE` trigger raises SQLSTATE 08006 (connection failure, which
# psycopg surfaces as an `OperationalError`) for the first `times` writes to the table; a
# sequence counts them outside any transaction, so a rolled-back attempt still counts.

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Literal

import psycopg
from psycopg import sql

__all__ = ["transient_outage"]


# meiva:note
# The first `times` `operation`s on `table` fail as a lost connection would; later ones work.
@contextmanager
def transient_outage(
    conn: psycopg.Connection,
    table: str,
    *,
    operation: Literal["INSERT", "UPDATE"] = "UPDATE",
    times: int = 1,
) -> Generator[None]:
    name = f"outage_{table}_{operation.lower()}"
    function = sql.Identifier(name)
    sequence = sql.Identifier(f"{name}_calls")
    target = sql.Identifier(table)
    conn.commit()
    conn.execute(sql.SQL("CREATE SEQUENCE {}").format(sequence))
    conn.execute(
        sql.SQL(
            "CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $body$ BEGIN "
            "IF nextval({sequence_name}) <= {times} THEN "
            "RAISE EXCEPTION 'temporary writer outage' USING ERRCODE = '08006'; END IF; "
            "RETURN NEW; END $body$"
        ).format(
            function=function,
            sequence_name=sql.Literal(name + "_calls"),
            times=sql.Literal(times),
        )
    )
    conn.execute(
        sql.SQL(
            "CREATE TRIGGER {function} BEFORE {operation} ON {target} "
            "FOR EACH ROW EXECUTE FUNCTION {function}()"
        ).format(function=function, operation=sql.SQL(operation), target=target)
    )
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        conn.execute(sql.SQL("DROP TRIGGER {} ON {}").format(function, target))
        conn.execute(sql.SQL("DROP FUNCTION {}()").format(function))
        conn.execute(sql.SQL("DROP SEQUENCE {}").format(sequence))
        conn.commit()
