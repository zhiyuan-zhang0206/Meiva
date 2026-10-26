from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from typing import Any, LiteralString, Protocol, overload


# meiva:note
# Minimal row-fetching surface returned by a synchronous database query execution.
class QueryResult(Protocol):
    def fetchall(self) -> list[tuple[Any, ...]]: ...

    def fetchone(self) -> tuple[Any, ...] | None: ...


# meiva:note
# Minimal synchronous database cursor contract for statement execution and row retrieval.
class SyncCursor[RowT](Protocol):
    def execute(
        self, query: LiteralString, params: Sequence[Any] | dict[str, Any] | None = None
    ) -> object: ...

    def executemany(self, query: LiteralString, params_seq: Iterable[Sequence[Any]]) -> object: ...

    def fetchall(self) -> list[RowT]: ...

    def fetchone(self) -> RowT | None: ...

    def __enter__(self) -> SyncCursor[RowT]: ...

    def __exit__(self, exc_type: None, exc_val: None, exc_tb: None) -> object: ...


# meiva:note
# Minimal synchronous database connection contract for query execution, cursors, and commits.
class SyncConnection(Protocol):
    def execute(
        self, query: LiteralString, params: Sequence[Any] | dict[str, Any] | None = None
    ) -> QueryResult: ...

    @overload
    def cursor(self) -> AbstractContextManager[SyncCursor[tuple[Any, ...]]]: ...

    @overload
    def cursor[RowT](
        self, *, row_factory: Callable[..., Callable[..., RowT]]
    ) -> AbstractContextManager[SyncCursor[RowT]]: ...

    def commit(self) -> None: ...


# meiva:note
# Minimal synchronous connection pool contract for borrowing a scoped connection.
class SyncPool(Protocol):
    def connection(
        self, timeout: float | None = None
    ) -> AbstractContextManager[SyncConnection]: ...

    def close(self) -> None: ...


# meiva:note
# Minimal synchronous database handle contract for opening connections, pools, and transactions.
class DatabaseLike(Protocol):
    def connect(self, *, autocommit: bool = False) -> AbstractContextManager[SyncConnection]: ...

    def pool(
        self,
        *,
        min_size: int | None = None,
        max_size: int | None = None,
        autocommit: bool = False,
    ) -> SyncPool: ...

    def write_transaction(self) -> AbstractContextManager[SyncConnection]: ...


# meiva:note
# Minimal row-fetching surface returned by an asynchronous database query execution.
class AsyncQueryResult(Protocol):
    @property
    def rowcount(self) -> int: ...

    def fetchall(self) -> Awaitable[list[tuple[Any, ...]]]: ...

    def fetchone(self) -> Awaitable[tuple[Any, ...] | None]: ...


# meiva:note
# Minimal asynchronous database connection contract for awaiting a query result.
class AsyncConnectionLike(Protocol):
    def execute(
        self, query: LiteralString, params: Sequence[Any] | dict[str, Any] | None = None
    ) -> Awaitable[AsyncQueryResult]: ...

    def transaction(self) -> AbstractAsyncContextManager[object]: ...


# meiva:note
# Minimal asynchronous connection pool contract for borrowing a scoped connection.
class AsyncPool(Protocol):
    def connection(
        self, timeout: float | None = None
    ) -> AbstractAsyncContextManager[AsyncConnectionLike]: ...
