from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass, field
from typing import LiteralString, Protocol, overload


class _DataPlaneView(Protocol):
    @property
    def db_url(self) -> str: ...

    @property
    def db_sslmode(self) -> str: ...

    @property
    def db_pool_min_size(self) -> int: ...

    @property
    def db_pool_max_size(self) -> int: ...

    @property
    def pgbouncer_enabled(self) -> bool: ...


class _EnvBootView(Protocol):
    @property
    def db_authority_refusal(self) -> str | None: ...


class _ViewSlice(_DataPlaneView, _EnvBootView, Protocol):
    pass


class _DirectView(Protocol):
    @property
    def data_plane(self) -> _DataPlaneView: ...

    @property
    def env_boot(self) -> _EnvBootView: ...


class _DynamicView(Protocol):
    def __getattr__(self, name: str) -> _ViewSlice: ...


class _ConfigBoot(Protocol):
    @property
    def view(self) -> _DirectView | _DynamicView: ...


@dataclass(frozen=True)
class DbConfig:
    db_url: str = field(repr=False)
    db_sslmode: str
    db_pool_min_size: int
    db_pool_max_size: int
    pgbouncer_enabled: bool
    db_authority_refusal: str | None = field(default=None, repr=False)


# meiva:note
# Raised when a pooled session restore ran past its client deadline. It is a
# TimeoutError so callers that treat a timed-out borrow as a retryable stall
# keep doing so.
class RestoreAbandonedError(TimeoutError):
    pass


def db_config_from_boot(boot: _ConfigBoot) -> DbConfig:
    view = boot.view
    data_plane = view.data_plane
    return DbConfig(
        db_url=data_plane.db_url,
        db_sslmode=data_plane.db_sslmode,
        db_pool_min_size=data_plane.db_pool_min_size,
        db_pool_max_size=data_plane.db_pool_max_size,
        pgbouncer_enabled=data_plane.pgbouncer_enabled,
        db_authority_refusal=view.env_boot.db_authority_refusal,
    )


# meiva:note
# Minimal row-fetching surface returned by a synchronous database query execution.
class QueryResult(Protocol):
    def fetchall(self) -> list[tuple[object, ...]]: ...

    def fetchone(self) -> tuple[object, ...] | None: ...


# meiva:note
# Minimal synchronous database cursor contract for statement execution and row retrieval.
class SyncCursor[RowT](Protocol):
    def execute(
        self, query: LiteralString, params: Sequence[object] | Mapping[str, object] | None = None
    ) -> SyncCursor[RowT]: ...

    def executemany(
        self, query: LiteralString, params_seq: Iterable[Sequence[object]]
    ) -> object: ...

    def fetchall(self) -> list[RowT]: ...

    def fetchone(self) -> RowT | None: ...

    def __enter__(self) -> SyncCursor[RowT]: ...

    def __exit__(self, exc_type: None, exc_val: None, exc_tb: None) -> object: ...


# meiva:note
# Minimal synchronous database connection contract for query execution, cursors, and commits.
class SyncConnection(Protocol):
    def execute(
        self, query: LiteralString, params: Sequence[object] | Mapping[str, object] | None = None
    ) -> QueryResult: ...

    @overload
    def cursor(self) -> AbstractContextManager[SyncCursor[tuple[object, ...]]]: ...

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

    def fetchall(self) -> Awaitable[list[tuple[object, ...]]]: ...

    def fetchone(self) -> Awaitable[tuple[object, ...] | None]: ...


# meiva:note
# Minimal asynchronous database cursor contract: statement execution and row retrieval, with the row type
# the cursor's row factory builds.
class AsyncCursor[RowT](Protocol):
    def execute(
        self, query: LiteralString, params: Sequence[object] | Mapping[str, object] | None = None
    ) -> Awaitable[AsyncCursor[RowT]]: ...

    def fetchall(self) -> Awaitable[list[RowT]]: ...

    def fetchone(self) -> Awaitable[RowT | None]: ...


# meiva:note
# Minimal asynchronous database connection contract for awaiting a query result, and for opening a cursor
# whose rows a row factory (`psycopg.rows.class_row(RowCls)`) builds as typed objects.
class AsyncConnectionLike(Protocol):
    def execute(
        self, query: LiteralString, params: Sequence[object] | Mapping[str, object] | None = None
    ) -> Awaitable[AsyncQueryResult]: ...

    @overload
    def cursor(self) -> AbstractAsyncContextManager[AsyncCursor[tuple[object, ...]]]: ...

    @overload
    def cursor[RowT](
        self, *, row_factory: Callable[..., Callable[..., RowT]]
    ) -> AbstractAsyncContextManager[AsyncCursor[RowT]]: ...

    def transaction(self) -> AbstractAsyncContextManager[object]: ...


# meiva:note
# Minimal asynchronous connection pool contract for borrowing a scoped connection.
class AsyncPool(Protocol):
    def connection(
        self, timeout: float | None = None
    ) -> AbstractAsyncContextManager[AsyncConnectionLike]: ...
