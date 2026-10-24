"""Explicit database transaction postures."""

from collections.abc import AsyncGenerator, Generator
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
from typing import Any, Protocol, TypeVar

import psycopg
from psycopg_pool import AsyncConnectionPool

_CT = TypeVar("_CT", bound=psycopg.AsyncConnection[Any])


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
def write_transaction(
    pool: ConnectionSource, *, timeout: float | None = None
) -> Generator[psycopg.Connection, None, None]:
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
async def async_write_transaction(  # noqa: UP047 - preserve the pool connection subtype.
    pool: AsyncConnectionPool[_CT], *, timeout: float | None = None
) -> AsyncGenerator[_CT, None]:
    """Borrow one async connection for an explicitly writable transaction.

    The session may default to read-only without this client asking (see
    `write_transaction`). Async pool connections use autocommit, so open a
    transaction before declaring it writable and yielding it for DML.
    """
    async with pool.connection(timeout=timeout) as conn, conn.transaction():
        await conn.execute("SET TRANSACTION READ WRITE")
        yield conn
