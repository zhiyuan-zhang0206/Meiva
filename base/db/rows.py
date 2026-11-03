from dataclasses import dataclass

import psycopg
from psycopg.abc import Params, QueryNoTemplate
from psycopg.rows import class_row


# meiva:note
# Run one query and hand back its first row as `row_class`, or None. `row_class` is the frozen
# dataclass (or pydantic model) declared next to the query, one field per selected column named as
# the column, so a reader gets typed attributes instead of `row[3]`. The cursor is opened and closed
# here, so a statement of a different shape in the same transaction gets its own row class.
def query_one[RowT, ConnRowT](
    conn: psycopg.Connection[ConnRowT],
    row_class: type[RowT],
    sql: QueryNoTemplate,
    params: Params | None = None,
) -> RowT | None:
    with conn.cursor(row_factory=class_row(row_class)) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


# meiva:note
# Run one query and hand back every row as `row_class`; see `query_one`.
def query_all[RowT, ConnRowT](
    conn: psycopg.Connection[ConnRowT],
    row_class: type[RowT],
    sql: QueryNoTemplate,
    params: Params | None = None,
) -> list[RowT]:
    with conn.cursor(row_factory=class_row(row_class)) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# meiva:note
# `query_one` on an async connection.
async def aquery_one[RowT, ConnRowT](
    conn: psycopg.AsyncConnection[ConnRowT],
    row_class: type[RowT],
    sql: QueryNoTemplate,
    params: Params | None = None,
) -> RowT | None:
    async with conn.cursor(row_factory=class_row(row_class)) as cur:
        await cur.execute(sql, params)
        return await cur.fetchone()


# meiva:note
# `query_all` on an async connection.
async def aquery_all[RowT, ConnRowT](
    conn: psycopg.AsyncConnection[ConnRowT],
    row_class: type[RowT],
    sql: QueryNoTemplate,
    params: Params | None = None,
) -> list[RowT]:
    async with conn.cursor(row_factory=class_row(row_class)) as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


# meiva:note
# The answer of a statement that selects (or `RETURNING`s) one `id` column.
@dataclass(frozen=True)
class IdRow:
    id: int


# meiva:note
# The answer of a `SELECT EXISTS (...) AS found` statement.
@dataclass(frozen=True)
class ExistsRow:
    found: bool


# meiva:note
# The answer of a `SELECT count(*) AS n` statement.
@dataclass(frozen=True)
class CountRow:
    n: int


# meiva:note
# Run a `SELECT count(*) AS n ...` statement and hand back the count. An aggregate always returns
# one row, so no row is a broken statement, not a zero.
def count_one(conn: psycopg.Connection, sql: QueryNoTemplate, params: Params | None = None) -> int:
    row = query_one(conn, CountRow, sql, params)
    if row is None:
        raise RuntimeError("an aggregate count returned no row")
    return row.n
