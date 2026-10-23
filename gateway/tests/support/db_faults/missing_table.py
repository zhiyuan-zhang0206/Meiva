# meiva:note
# A table that is not there, produced by the throwaway Postgres itself.
#
# A read that must fall back (or fail) when its table is unavailable is tested against the real
# error: while the context is open the table is renamed away, so every query that names it
# raises `UndefinedTable` exactly as it would against a database missing the table.

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import psycopg
from psycopg import sql

__all__ = ["VanishingTable", "missing_table", "vanishing_table"]


# meiva:note
# `table` does not exist for the duration of the block.
@contextmanager
def missing_table(conn: psycopg.Connection, table: str) -> Generator[None]:
    hidden = sql.Identifier(f"{table}_hidden_by_test")
    target = sql.Identifier(table)
    conn.commit()
    conn.execute(
        sql.SQL("ALTER TABLE {target} RENAME TO {hidden}").format(target=target, hidden=hidden)
    )
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        conn.execute(
            sql.SQL("ALTER TABLE {hidden} RENAME TO {target}").format(target=target, hidden=hidden)
        )
        conn.commit()


# meiva:note
# A table that disappears in the middle of a request, when a scripted party decides.
class VanishingTable:
    def __init__(self, db_url: str, table: str) -> None:
        self._db_url = db_url
        self._table = table
        self._hidden = f"{table}_vanished_by_test"

    def vanish(self) -> None:
        with psycopg.connect(self._db_url, autocommit=True) as conn:
            conn.execute(
                sql.SQL("ALTER TABLE {} RENAME TO {}").format(
                    sql.Identifier(self._table), sql.Identifier(self._hidden)
                )
            )

    def restore(self) -> None:
        with psycopg.connect(self._db_url, autocommit=True) as conn:
            conn.execute(
                sql.SQL("ALTER TABLE IF EXISTS {} RENAME TO {}").format(
                    sql.Identifier(self._hidden), sql.Identifier(self._table)
                )
            )


# meiva:note
# `table` can be made to vanish mid-block (`vanish()`); it is back when the block ends.
@contextmanager
def vanishing_table(db_url: str, table: str) -> Generator[VanishingTable]:
    handle = VanishingTable(db_url, table)
    try:
        yield handle
    finally:
        handle.restore()
