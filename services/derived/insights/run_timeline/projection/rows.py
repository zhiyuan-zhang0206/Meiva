# meiva:note
# Typed rows for a `SyncConnection` read.
#
# `rows_as(Shape)` is a `row_factory` for `conn.cursor(...)`: each row arrives as a tuple and is
# validated into `Shape`, a NamedTuple declared beside the query with one field per selected column
# in select order, so a reader gets typed attributes instead of `row[3]` and a column of the wrong
# type fails at the read.

from __future__ import annotations

from collections.abc import Callable, Sequence

from pydantic import TypeAdapter


def rows_as[R](shape: type[R]) -> Callable[[object], Callable[[Sequence[object]], R]]:
    adapter = TypeAdapter(shape)

    def factory(cursor: object) -> Callable[[Sequence[object]], R]:
        del cursor

        def make(values: Sequence[object]) -> R:
            return adapter.validate_python(tuple(values))

        return make

    return factory
