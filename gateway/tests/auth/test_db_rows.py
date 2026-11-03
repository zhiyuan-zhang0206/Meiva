from dataclasses import dataclass

import psycopg

from gateway.http.db_rows import query_all, query_one
from gateway.http.json_values import is_object_dict, is_object_list


@dataclass(frozen=True)
class _Pair:
    left: int
    right: str


def test_query_one_hands_back_the_row_class_or_none(db_conn: psycopg.Connection) -> None:
    sql = "SELECT %s::int AS left, 'two' AS right WHERE %s::int > 0"
    assert query_one(db_conn, _Pair, sql, (1, 1)) == _Pair(left=1, right="two")
    assert query_one(db_conn, _Pair, sql, (1, 0)) is None


def test_query_all_hands_back_every_row_in_order(db_conn: psycopg.Connection) -> None:
    rows = query_all(
        db_conn,
        _Pair,
        "SELECT i AS left, 'x' || i AS right FROM generate_series(1, 3) AS i ORDER BY i",
    )
    assert rows == [_Pair(1, "x1"), _Pair(2, "x2"), _Pair(3, "x3")]


def test_each_query_gets_its_own_row_class_in_one_transaction(db_conn: psycopg.Connection) -> None:
    @dataclass(frozen=True)
    class Solo:
        n: int

    assert query_one(db_conn, Solo, "SELECT 7 AS n") == Solo(7)
    assert query_one(db_conn, _Pair, "SELECT 1 AS left, 'a' AS right") == _Pair(1, "a")


def test_object_guards_accept_only_objects_and_lists() -> None:
    assert is_object_dict({"a": 1})
    assert not is_object_dict({1: "a"})
    assert not is_object_dict([("a", 1)])
    assert is_object_list([1, "a"])
    assert not is_object_list(("a",))
