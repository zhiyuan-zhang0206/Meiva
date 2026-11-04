# meiva:note
# Stand-ins for a connection whose cursors build rows through a row factory. psycopg's `class_row`
# reads the column names off the cursor's `pgresult` and its `_encoding`, so a fake that answers
# plain tuples cannot be handed a `row_factory`; these carry the names the statement would select
# and apply the factory the way a real cursor does. One copy of that knowledge lives here instead
# of one per test file. `FakeConnection` is the sync face (`SyncConnection.cursor`),
# `FakeAsyncConnection` the async one (`AsyncConnectionLike.cursor`).
from __future__ import annotations

from collections.abc import Callable, Sequence
from types import TracebackType

import psycopg

__all__ = ["FakeAsyncConnection", "FakeConnection", "RowFactory"]

type RowFactory = Callable[..., Callable[..., object]]


class _FakeResult:
    status = psycopg.pq.ExecStatus.TUPLES_OK

    def __init__(self, names: Sequence[str]) -> None:
        self._names = tuple(names)
        self.nfields = len(self._names)

    def fname(self, index: int) -> bytes:
        return self._names[index].encode()


# meiva:note
# One cursor over the rows a statement would answer, named by `names`. `row_factory` is applied to
# each row when it is fetched; without one the rows come back as the tuples they were given.
class FakeCursor:
    _encoding = "utf-8"

    def __init__(
        self,
        names: Sequence[str],
        rows: Sequence[tuple[object, ...]],
        row_factory: RowFactory | None,
        statements: list[tuple[str, object]],
    ) -> None:
        self.pgresult = _FakeResult(names)
        self._rows = list(rows)
        self._row_factory = row_factory
        self._statements = statements

    def _build(self, row: tuple[object, ...]) -> object:
        return row if self._row_factory is None else self._row_factory(self)(row)

    def _execute(self, query: str, params: object) -> FakeCursor:
        self._statements.append((query, params))
        return self

    def _fetchone(self) -> object | None:
        return self._build(self._rows.pop(0)) if self._rows else None

    def _fetchall(self) -> list[object]:
        rows, self._rows = self._rows, []
        return [self._build(row) for row in rows]


class _SyncCursor(FakeCursor):
    def execute(self, query: str, params: object = None) -> FakeCursor:
        return self._execute(query, params)

    def fetchone(self) -> object | None:
        return self._fetchone()

    def fetchall(self) -> list[object]:
        return self._fetchall()

    def __enter__(self) -> _SyncCursor:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class _AsyncCursor(FakeCursor):
    async def execute(self, query: str, params: object = None) -> FakeCursor:
        return self._execute(query, params)

    async def fetchone(self) -> object | None:
        return self._fetchone()

    async def fetchall(self) -> list[object]:
        return self._fetchall()

    async def __aenter__(self) -> _AsyncCursor:
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


# meiva:note
# A sync connection answering `rows` under the column `names`, whatever the statement. `statements`
# records every `(query, params)` a cursor executed, in order.
class FakeConnection:
    def __init__(self, names: Sequence[str], rows: Sequence[tuple[object, ...]]) -> None:
        self._names = tuple(names)
        self._rows = tuple(rows)
        self.statements: list[tuple[str, object]] = []

    def cursor(self, *, row_factory: RowFactory | None = None) -> _SyncCursor:
        return _SyncCursor(self._names, self._rows, row_factory, self.statements)

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


# meiva:note
# The async twin of `FakeConnection`: `cursor()` opens an async cursor whose `execute` and fetches are awaited.
class FakeAsyncConnection:
    def __init__(self, names: Sequence[str], rows: Sequence[tuple[object, ...]]) -> None:
        self._names = tuple(names)
        self._rows = tuple(rows)
        self.statements: list[tuple[str, object]] = []

    def cursor(self, *, row_factory: RowFactory | None = None) -> _AsyncCursor:
        return _AsyncCursor(self._names, self._rows, row_factory, self.statements)
