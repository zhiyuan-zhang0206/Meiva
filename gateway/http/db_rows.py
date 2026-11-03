from dataclasses import dataclass

import psycopg
from psycopg.abc import Params, QueryNoTemplate
from psycopg.rows import class_row


# meiva:note
# Run one query and hand back its first row as `row_class`, or None. `row_class` is the frozen
# dataclass (or pydantic model) declared next to the query, one field per selected column named as
# the column, so a reader gets typed attributes instead of `row[3]`. The cursor is opened and closed
# here, so a statement of a different shape in the same transaction gets its own row class.
def query_one[RowT](
    conn: psycopg.Connection,
    row_class: type[RowT],
    sql: QueryNoTemplate,
    params: Params | None = None,
) -> RowT | None:
    with conn.cursor(row_factory=class_row(row_class)) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


# meiva:note
# Run one query and hand back every row as `row_class`; see `query_one`.
def query_all[RowT](
    conn: psycopg.Connection,
    row_class: type[RowT],
    sql: QueryNoTemplate,
    params: Params | None = None,
) -> list[RowT]:
    with conn.cursor(row_factory=class_row(row_class)) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


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
