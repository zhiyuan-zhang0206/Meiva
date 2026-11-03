from dataclasses import dataclass

import psycopg
import pytest
from psycopg.rows import class_row

from base.db import fetch_one


@dataclass(frozen=True)
class _Pair:
    left: int
    right: str


def test_class_row_cursor_hands_back_the_row_class(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor(row_factory=class_row(_Pair)) as cur:
        cur.execute("SELECT 1 AS left, 'two' AS right")
        row = fetch_one(cur, "pair")
    assert row == _Pair(left=1, right="two")


def test_plain_cursor_still_hands_back_a_tuple(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute("SELECT 1, 'two'")
        row = fetch_one(cur, "pair")
    assert row == (1, "two")


def test_no_row_names_the_caller_context(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor(row_factory=class_row(_Pair)) as cur:
        cur.execute("SELECT 1 AS left, 'two' AS right WHERE false")
        with pytest.raises(RuntimeError, match="expected exactly one row: empty pair"):
            fetch_one(cur, "empty pair")
