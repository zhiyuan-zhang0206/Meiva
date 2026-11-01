"""Checkpoint cursors own a pool lease instead of a host-wide saver lock."""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from copy import copy
from typing import TypeGuard
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.base import SerializerProtocol
from psycopg import AsyncConnection, AsyncCursor
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from base.agents.history.checkpoint_postgres_walks import HistoryAsyncPostgresSaver
from base.native_process.runtime_incarnation import (
    RuntimeIncarnation,
    StaleIncarnationError,
)


def _is_dict_row_pool(
    _pool: AsyncConnectionPool[AsyncConnection],
) -> TypeGuard[AsyncConnectionPool[AsyncConnection[DictRow]]]:
    return True


class MissingCheckpointParentError(RuntimeError):
    """A hosted checkpoint write names a parent absent from its exact thread/namespace."""


def _parse_thread_agent_id(thread_id: object | None, generation: UUID | None) -> int | None:
    if thread_id is None:
        if generation is not None:
            raise StaleIncarnationError(
                f"runtime generation {generation} specified but config has no thread_id"
            )
        return None
    try:
        return int(str(thread_id))
    except (ValueError, TypeError) as exc:
        if generation is not None:
            raise StaleIncarnationError(
                f"runtime generation {generation} specified for non-agent thread {thread_id!r}"
            ) from exc
        return None


def _check_generation_match(
    agent_id: int,
    row_generation: object,
    row_status: object,
    write_generation: UUID | None,
) -> None:
    row_uuid = (
        row_generation
        if isinstance(row_generation, UUID)
        else (UUID(str(row_generation)) if row_generation is not None else None)
    )
    if write_generation is not None:
        if row_status != "running" or row_uuid != write_generation:
            raise StaleIncarnationError(
                f"stale checkpoint write: agent {agent_id} has generation {row_generation} "
                f"and status {row_status!r}, but write held generation {write_generation}"
            )
    elif row_generation is not None or row_status == "running":
        raise StaleIncarnationError(
            f"checkpoint write for active agent {agent_id} (status={row_status!r}, "
            f"generation={row_generation}) requires runtime_generation fencing token"
        )


# meiva:note
# One logical saver, with independent connections for concurrent operations.
#
# LangGraph's pooled saver takes its instance lock before borrowing a
# connection, serializing unrelated agents. Lease the connection first and
# delegate its cursor to a short-lived upstream saver: the lock now belongs
# to that exclusive lease. Upstream still owns pipeline synchronization,
# transaction fallback, and cursor cleanup on errors or cancellation.
#
# Checkpoint and writes persistence validate their exact parent and
# fencing token on the same lease and transaction as the upstream writes.
# Before writing checkpoint, blob, or intermediate write rows, the write
# transaction atomically verifies against `agents_meta` with `FOR SHARE` that
# the admitted runner's `runtime_generation` is still valid and `running`. A
# stale generation (or an active agent write lacking the required fencing token)
# fails closed immediately with `StaleIncarnationError`, rolling back the
# transaction and persisting zero rows.
class PooledPostgresSaver(HistoryAsyncPostgresSaver):
    def __init__(
        self,
        conn: AsyncConnectionPool[AsyncConnection[DictRow]],
        *,
        serde: SerializerProtocol | None = None,
        incarnation: RuntimeIncarnation | None = None,
        runtime_generation: UUID | None = None,
    ) -> None:
        super().__init__(conn=conn, serde=serde)
        self._pool = conn
        self._incarnation = incarnation
        self._runtime_generation = runtime_generation or (
            incarnation.generation if incarnation is not None else None
        )

    @classmethod
    def from_pool(
        cls,
        pool: AsyncConnectionPool[AsyncConnection],
        *,
        serde: SerializerProtocol | None = None,
        incarnation: RuntimeIncarnation | None = None,
        runtime_generation: UUID | None = None,
    ) -> "PooledPostgresSaver":
        if not _is_dict_row_pool(pool):
            raise TypeError("expected DictRow pool")
        return cls(
            conn=pool,
            serde=serde,
            incarnation=incarnation,
            runtime_generation=runtime_generation,
        )

    def with_incarnation(self, incarnation: RuntimeIncarnation | None) -> "PooledPostgresSaver":
        saver = copy(self)
        saver._incarnation = incarnation
        saver._runtime_generation = incarnation.generation if incarnation is not None else None
        return saver

    def _resolve_generation(self, config: RunnableConfig | None) -> UUID | None:
        if config is not None:
            configurable = config.get("configurable", {})
            if "runtime_generation" in configurable:
                gen = configurable["runtime_generation"]
                if gen is not None:
                    return gen if isinstance(gen, UUID) else UUID(str(gen))
            if "runtime_incarnation" in configurable:
                inc = configurable["runtime_incarnation"]
                if isinstance(inc, RuntimeIncarnation):
                    return inc.generation
                if inc is not None and hasattr(inc, "generation"):
                    gen = inc.generation
                    return gen if isinstance(gen, UUID) else UUID(str(gen))
        return self._runtime_generation

    async def _verify_fencing(
        self,
        conn: AsyncConnection[DictRow],
        thread_id: object | None,
        generation: UUID | None,
    ) -> None:
        agent_id = _parse_thread_agent_id(thread_id, generation)
        if agent_id is None:
            return
        if self._incarnation is not None:
            self._incarnation.require_agent(agent_id)

        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT runtime_generation, status FROM agents_meta WHERE id = %s FOR SHARE",
                (agent_id,),
            )
            row = await cur.fetchone()
            if row is None:
                if generation is not None:
                    raise StaleIncarnationError(
                        f"stale checkpoint write: agent {agent_id} not found in agents_meta "
                        f"(expected generation {generation})"
                    )
                return

            row_generation = row["runtime_generation"] if isinstance(row, dict) else row[0]
            row_status = row["status"] if isinstance(row, dict) else row[1]
            _check_generation_match(agent_id, row_generation, row_status, generation)

    # meiva:note
    # Commit one checkpoint only after verifying fencing token and named parent.
    # The row lock on `agents_meta` (`FOR SHARE`) prevents generation rotation or status
    # change between this check and commit. The parent lock (`FOR KEY SHARE`) prevents parent
    # deletion. Upstream owns serialization and inserts, with all blob/checkpoint writes
    # in this atomic transaction. First checkpoints may have no parent.
    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        configurable = config.get("configurable", {})
        thread_id = configurable.get("thread_id")
        parent = configurable.get("checkpoint_id")
        generation = self._resolve_generation(config)
        async with self._pool.connection() as conn, conn.transaction():
            await self._verify_fencing(conn, thread_id, generation)
            if parent is not None:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT 1 FROM checkpoints WHERE thread_id=%s "
                        "AND checkpoint_ns=%s AND checkpoint_id=%s FOR KEY SHARE",
                        (
                            configurable["thread_id"],
                            configurable.get("checkpoint_ns", ""),
                            parent,
                        ),
                    )
                    if await cur.fetchone() is None:
                        raise MissingCheckpointParentError(
                            f"checkpoint parent is missing: thread={configurable['thread_id']} "
                            f"namespace={configurable.get('checkpoint_ns', '')!r} parent={parent}"
                        )
            lease = AsyncPostgresSaver(conn=conn, serde=self.serde)
            res = await lease.aput(config, checkpoint, metadata, new_versions)
            return {
                **res,
                "configurable": {
                    **configurable,
                    **res.get("configurable", {}),
                },
            }

    # meiva:note
    # Commit intermediate checkpoint writes after verifying fencing token.
    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, object]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        configurable = config.get("configurable", {})
        thread_id = configurable.get("thread_id")
        generation = self._resolve_generation(config)
        async with self._pool.connection() as conn, conn.transaction():
            await self._verify_fencing(conn, thread_id, generation)
            lease = AsyncPostgresSaver(conn=conn, serde=self.serde)
            await lease.aput_writes(config, writes, task_id, task_path)

    # meiva:note
    # Delete thread checkpoints after verifying fencing token.
    async def adelete_thread(self, thread_id: str) -> None:
        generation = self._resolve_generation(None)
        async with self._pool.connection() as conn, conn.transaction():
            await self._verify_fencing(conn, thread_id, generation)
            lease = AsyncPostgresSaver(conn=conn, serde=self.serde)
            await lease.adelete_thread(thread_id)

    @asynccontextmanager
    async def _cursor(self, *, pipeline: bool = False) -> AsyncGenerator[AsyncCursor[DictRow]]:
        async with self._pool.connection() as conn:
            lease = AsyncPostgresSaver(conn=conn, serde=self.serde)
            async with lease._cursor(pipeline=pipeline) as cur:
                yield cur
