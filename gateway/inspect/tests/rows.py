from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.abc import Params, QueryNoTemplate

from gateway.http.db_rows import query_one


# meiva:note
# The `id` column a test's `INSERT ... RETURNING id` or `SELECT id` hands back.
@dataclass(frozen=True)
class IdRow:
    id: int


# meiva:note
# Run one statement that returns a single `id` and hand it back; the statement not returning a row
# is a broken test setup.
def returned_id(
    conn: psycopg.Connection, sql: QueryNoTemplate, params: Params | None = None
) -> int:
    row = query_one(conn, IdRow, sql, params)
    assert row is not None
    return row.id
