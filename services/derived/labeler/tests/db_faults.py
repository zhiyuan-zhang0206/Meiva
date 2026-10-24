# meiva:note
# A table that is not there, produced by the throwaway Postgres itself.
#
# While the block is open the table is renamed away and the rename is committed, so every statement
# that names it raises `UndefinedTable` on any connection, exactly as it would against a database
# missing the table. The name is restored when the block ends.
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

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
