"""Checkpoint cursors own a pool lease instead of a host-wide saver lock."""

from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from copy import copy
from typing import Any, TypeGuard
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.base import SerializerProtocol
from psycopg import AsyncConnection, AsyncCursor
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from base.agents.history.checkpoint_postgres_walks import HistoryAsyncPostgresSaver
from base.native_process.runtime_incarnation import StaleIncarnationError


def _is_dict_row_pool(
    _pool: AsyncConnectionPool[AsyncConnection],
) -> TypeGuard[AsyncConnectionPool[AsyncConnection[DictRow]]]:
    return True


class MissingCheckpointParentError(RuntimeError):
    """A hosted checkpoint write names a parent absent from its exact thread/namespace."""


def _check_row_ownership(
    agent_id: int,
    db_machine: str,
    db_owner: UUID | None,
    db_status: str,
    *,
    expected_owner: UUID,
    expected_machine: str | None,
    for_delete: bool,
) -> None:
    if db_status == "terminated":
        if for_delete:
            return
        raise StaleIncarnationError(f"cannot write checkpoint for terminated agent {agent_id}")

    if db_status == "held":
        return

    if db_owner is not None:
        if db_owner != expected_owner:
            raise StaleIncarnationError(
                f"stale runner write: expected owner {expected_owner}, "
                f"but agents_meta has owner {db_owner} (machine {db_machine})"
            )
        return

    if expected_machine is not None and db_machine != expected_machine:
        raise StaleIncarnationError(
            f"stale runner write: agent {agent_id} is on machine {db_machine}, "
            f"not expected machine {expected_machine}"
        )
    if db_status == "running":
        raise StaleIncarnationError(
            f"stale runner write: agent {agent_id} is marked running without an owner"
        )


class PooledPostgresSaver(HistoryAsyncPostgresSaver):
    """One logical saver, with independent connections for concurrent operations.

    LangGraph's pooled saver takes its instance lock before borrowing a
    connection, serializing unrelated agents. Lease the connection first and
    delegate its cursor to a short-lived upstream saver: the lock now belongs
    to that exclusive lease. Upstream still owns pipeline synchronization,
    transaction fallback, and cursor cleanup on errors or cancellation.

    Checkpoint writes also validate their exact parent on the same lease and
    transaction as the upstream blob/checkpoint inserts. The host's N-step
    wrapper first reparents coalesced saves and serializes each thread's writes
    and flushes. A missing parent is a failed save, never a dangling successor.
    """

    def __init__(
        self,
        conn: AsyncConnectionPool[AsyncConnection[DictRow]],
        *,
        serde: SerializerProtocol | None = None,
        expected_owner: UUID | None = None,
        machine: str | None = None,
    ) -> None:
        super().__init__(conn=conn, serde=serde)
        self._pool = conn
        self._expected_owner = expected_owner
        self._machine = machine

    @classmethod
    def from_pool(
        cls,
        pool: AsyncConnectionPool[AsyncConnection],
        *,
        serde: SerializerProtocol | None = None,
        expected_owner: UUID | None = None,
        machine: str | None = None,
    ) -> "PooledPostgresSaver":
        if not _is_dict_row_pool(pool):
            raise TypeError("expected DictRow pool")
        return cls(conn=pool, serde=serde, expected_owner=expected_owner, machine=machine)

    @property
    def expected_owner(self) -> UUID | None:
        return self._expected_owner

    @property
    def machine(self) -> str | None:
        return self._machine

    def with_owner(
        self, expected_owner: UUID | None, *, machine: str | None = None
    ) -> "PooledPostgresSaver":
        bound = copy(self)
        bound._expected_owner = expected_owner
        bound._machine = machine
        return bound

    async def _verify_owner(
        self,
        conn: AsyncConnection[DictRow],
        thread_id: str,
        *,
        for_delete: bool = False,
    ) -> None:
        if self._expected_owner is None:
            return

        try:
            agent_id = int(thread_id)
        except (ValueError, TypeError):
            return

        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT machine, runtime_owner, status FROM agents_meta WHERE id = %s FOR SHARE",
                (agent_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return

            if isinstance(row, Mapping):
                raw_machine = row["machine"]
                raw_owner = row["runtime_owner"]
                raw_status = row["status"]
            else:
                raw_machine = row[0]
                raw_owner = row[1]
                raw_status = row[2]

            db_owner = (
                raw_owner
                if isinstance(raw_owner, UUID) or raw_owner is None
                else UUID(str(raw_owner))
            )
            _check_row_ownership(
                agent_id,
                str(raw_machine),
                db_owner,
                str(raw_status),
                expected_owner=self._expected_owner,
                expected_machine=self._machine,
                for_delete=for_delete,
            )

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Commit one checkpoint only after its named parent exists.

        The row lock prevents deletion between this check and commit; it is not
        a foreign key or a guarantee against later retention. Upstream owns
        serialization and inserts, with all blob/checkpoint writes in this
        transaction. First checkpoints may have no parent.
        """
        configurable = config.get("configurable", {})
        thread_id = configurable.get("thread_id", "")
        parent = configurable.get("checkpoint_id")
        async with self._pool.connection() as conn, conn.transaction():
            await self._verify_owner(conn, thread_id)
            if parent is not None:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT 1 FROM checkpoints WHERE thread_id=%s "
                        "AND checkpoint_ns=%s AND checkpoint_id=%s FOR KEY SHARE",
                        (thread_id, configurable["checkpoint_ns"], parent),
                    )
                    if await cur.fetchone() is None:
                        raise MissingCheckpointParentError(
                            f"checkpoint parent is missing: thread={thread_id} "
                            f"namespace={configurable['checkpoint_ns']!r} parent={parent}"
                        )
            lease = AsyncPostgresSaver(conn=conn, serde=self.serde)
            return await lease.aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        configurable = config.get("configurable", {})
        thread_id = configurable.get("thread_id", "")
        async with self._pool.connection() as conn, conn.transaction():
            await self._verify_owner(conn, thread_id)
            lease = AsyncPostgresSaver(conn=conn, serde=self.serde)
            await lease.aput_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await self._verify_owner(conn, thread_id, for_delete=True)
            lease = AsyncPostgresSaver(conn=conn, serde=self.serde)
            await lease.adelete_thread(thread_id)

    @asynccontextmanager
    async def _cursor(self, *, pipeline: bool = False) -> AsyncGenerator[AsyncCursor[DictRow]]:
        async with self._pool.connection() as conn:
            lease = AsyncPostgresSaver(conn=conn, serde=self.serde)
            async with lease._cursor(pipeline=pipeline) as cur:
                yield cur
