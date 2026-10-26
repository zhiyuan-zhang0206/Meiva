"""Explicit database transaction postures."""

from collections.abc import AsyncGenerator, Generator
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    asynccontextmanager,
    contextmanager,
)
from typing import Any, Protocol

import psycopg

from base.db.protocols import AsyncConnectionLike, SyncConnection


# meiva:note
# Generic async connection-lending contract preserving the connection subtype across transactions.
class _AsyncTransactionPool[CT: AsyncConnectionLike](Protocol):
    def connection(self, timeout: float | None = None) -> AbstractAsyncContextManager[CT]: ...


# meiva:note
# Generic sync connection-lending contract preserving the connection subtype across transactions.
class _SyncTransactionPool[SyncCT: SyncConnection](Protocol):
    def connection(self, timeout: float | None = None) -> AbstractContextManager[SyncCT]: ...


# meiva:note
# What an async reader needs of a pool: one connection lent for the length of an `async with`
# block. `psycopg_pool.AsyncConnectionPool` is one; a test pool that swallows a cancellation of the
# borrow (`base/db/tests/swallowed_cancel.py`) is the other, a fault no real pool produces on demand.
class AsyncConnectionSource(Protocol):
    def connection(
        self, timeout: float | None = None
    ) -> AbstractAsyncContextManager[psycopg.AsyncConnection[Any]]: ...


# meiva:note
# What a transaction needs of a pool: one connection lent for the length of a `with` block, committed
# when the block ends normally. `psycopg_pool.ConnectionPool` is one; a test pool that loses the
# acknowledgement of a commit it really made is the other (`base/db/tests/lost_ack.py`), which no
# real pool can be made to do on demand.
class ConnectionSource(Protocol):
    def connection(
        self, timeout: float | None = None
    ) -> AbstractContextManager[psycopg.Connection[Any]]: ...


@contextmanager
def write_transaction[SyncCT: SyncConnection](
    pool: _SyncTransactionPool[SyncCT], *, timeout: float | None = None
) -> Generator[SyncCT, None, None]:
    """Open one transaction explicitly allowed to write, on a connection of `pool`.

    The session may default to read-only without this client asking: a pooler
    that does not track `default_transaction_read_only` per client (PgBouncer
    before 1.26; a remote-managed data plane's pooler is not ours to pin) hands
    over what another client SET, and a database or role default survives the
    pooled session scrub. Declare this transaction writable as its first
    statement before DML, pinning the borrowed backend until the context
    commits or rolls back. Pool connections must not use autocommit.
    """
    with pool.connection(timeout=timeout) as conn:
        conn.execute("SET TRANSACTION READ WRITE")
        yield conn


@asynccontextmanager
async def async_write_transaction[CT: AsyncConnectionLike](
    pool: _AsyncTransactionPool[CT], *, timeout: float | None = None
) -> AsyncGenerator[CT, None]:
    """Borrow one async connection for an explicitly writable transaction.

    The session may default to read-only without this client asking (see
    `write_transaction`). Async pool connections use autocommit, so open a
    transaction before declaring it writable and yielding it for DML.
    """
    async with pool.connection(timeout=timeout) as conn, conn.transaction():
        await conn.execute("SET TRANSACTION READ WRITE")
        yield conn
