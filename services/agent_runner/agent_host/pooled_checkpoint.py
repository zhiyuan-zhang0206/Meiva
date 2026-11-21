"""Checkpoint cursors own a pool lease instead of a host-wide saver lock."""

import asyncio
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from copy import copy
from dataclasses import dataclass
from typing import LiteralString, TypeGuard
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.postgres.base import BasePostgresSaver
from langgraph.checkpoint.serde.base import SerializerProtocol
from opentelemetry import trace as otel_trace
from psycopg import AsyncConnection, AsyncCursor
from psycopg.rows import DictRow
from psycopg_pool import AsyncConnectionPool

from base.agents.history.checkpoint_postgres_walks import HistoryAsyncPostgresSaver
from base.db.rows import aquery_one
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_mapping, object_of
from base.native_process.runtime_incarnation import StaleIncarnationError

# meiva:note
# Default 64 MiB byte cap mirrors `DaemonSettings.host_checkpoint_blob_cache_mb` (32 warm agents x ~2 MiB).
_DEFAULT_BLOB_CACHE_MAX_BYTES = 64 * 1024 * 1024

SELECT_COLUMNS: LiteralString = """
select
    thread_id,
    checkpoint,
    checkpoint_ns,
    checkpoint_id,
    parent_checkpoint_id,
    metadata,
    (
        select array_agg(array[bl.channel::bytea, bl.type::bytea, bl.blob])
        from jsonb_each_text(checkpoint -> 'channel_versions')
        inner join checkpoint_blobs bl
            on bl.thread_id = checkpoints.thread_id
            and bl.checkpoint_ns = checkpoints.checkpoint_ns
            and bl.channel = jsonb_each_text.key
            and bl.version = jsonb_each_text.value
    ) as channel_values,
    (
        select
        array_agg(array[cw.task_id::text::bytea, cw.channel::bytea, cw.type::bytea, cw.blob, convert_to(cw.task_path, 'UTF8'), cw.idx::text::bytea])
        from checkpoint_writes cw
        where cw.thread_id = checkpoints.thread_id
            and cw.checkpoint_ns = checkpoints.checkpoint_ns
            and cw.checkpoint_id = checkpoints.checkpoint_id
    ) as pending_writes
from checkpoints """

_SELECT_EXCLUDING_CACHED_COLUMNS: LiteralString = """
select
    thread_id,
    checkpoint,
    checkpoint_ns,
    checkpoint_id,
    parent_checkpoint_id,
    metadata,
    (
        select array_agg(array[bl.channel::bytea, bl.type::bytea, bl.blob])
        from jsonb_each_text(checkpoint -> 'channel_versions')
        inner join checkpoint_blobs bl
            on bl.thread_id = checkpoints.thread_id
            and bl.checkpoint_ns = checkpoints.checkpoint_ns
            and bl.channel = jsonb_each_text.key
            and bl.version = jsonb_each_text.value
        where not exists (
            select 1
            from unnest(%s::text[], %s::text[]) as cached(ch, ver)
            where cached.ch = jsonb_each_text.key and cached.ver = jsonb_each_text.value
        )
    ) as channel_values,
    (
        select
        array_agg(array[cw.task_id::text::bytea, cw.channel::bytea, cw.type::bytea, cw.blob, convert_to(cw.task_path, 'UTF8'), cw.idx::text::bytea])
        from checkpoint_writes cw
        where cw.thread_id = checkpoints.thread_id
            and cw.checkpoint_ns = checkpoints.checkpoint_ns
            and cw.checkpoint_id = checkpoints.checkpoint_id
    ) as pending_writes
from checkpoints """

_WHERE_BY_ID: LiteralString = "WHERE thread_id = %s AND checkpoint_ns = %s AND checkpoint_id = %s"
_WHERE_LATEST: LiteralString = (
    "WHERE thread_id = %s AND checkpoint_ns = %s ORDER BY checkpoint_id DESC LIMIT 1"
)
_SELECT_BY_ID: LiteralString = SELECT_COLUMNS + _WHERE_BY_ID
_SELECT_LATEST: LiteralString = SELECT_COLUMNS + _WHERE_LATEST
_SELECT_EXCLUDING_BY_ID: LiteralString = _SELECT_EXCLUDING_CACHED_COLUMNS + _WHERE_BY_ID
_SELECT_EXCLUDING_LATEST: LiteralString = _SELECT_EXCLUDING_CACHED_COLUMNS + _WHERE_LATEST


def _tuple_query(
    thread_id: str,
    checkpoint_ns: str,
    checkpoint_id: str | None,
    cached: Mapping[str, tuple[str, bytes, bytes]],
) -> tuple[LiteralString, tuple[object, ...]]:
    if checkpoint_id:
        where_args: tuple[object, ...] = (thread_id, checkpoint_ns, checkpoint_id)
        query = _SELECT_EXCLUDING_BY_ID if cached else _SELECT_BY_ID
    else:
        where_args = (thread_id, checkpoint_ns)
        query = _SELECT_EXCLUDING_LATEST if cached else _SELECT_LATEST
    if not cached:
        return query, where_args
    return query, (list(cached), [entry[0] for entry in cached.values()], *where_args)


def _needs_legacy_sends_migration(row_map: Mapping[str, object]) -> str | None:
    raw_cp = row_map.get("checkpoint")
    checkpoint_map = raw_cp if is_object_dict(raw_cp) else {}
    version = checkpoint_map.get("v")
    parent_id = row_map.get("parent_checkpoint_id")
    if isinstance(version, int) and version < 4 and isinstance(parent_id, str) and parent_id:
        return parent_id
    return None


def _pending_sends_pairs(raw: object) -> list[tuple[bytes, bytes]]:
    if not is_object_list(raw):
        return []
    pairs: list[tuple[bytes, bytes]] = []
    for item in raw:
        if (
            is_object_list(item)
            and len(item) == 2
            and isinstance(item[0], bytes)
            and isinstance(item[1], bytes)
        ):
            pairs.append((item[0], item[1]))
    return pairs


def _channel_versions_of(row_map: Mapping[str, object]) -> Mapping[str, object]:
    raw_cp = row_map.get("checkpoint")
    checkpoint_map = raw_cp if is_object_dict(raw_cp) else {}
    raw_versions = checkpoint_map.get("channel_versions")
    return raw_versions if is_object_dict(raw_versions) else {}


def _record_tuple_span(
    span: otel_trace.Span,
    *,
    thread_id: str,
    sql_ms: float,
    decode_ms: float,
    cache_hits: int,
    cache_hit_bytes: int,
    fetched_bytes: int,
) -> None:
    if not span.is_recording():
        return
    span.set_attribute("checkpoint.thread_id", thread_id)
    span.set_attribute("checkpoint.sql_ms", round(sql_ms, 2))
    span.set_attribute("checkpoint.decode_ms", round(decode_ms, 2))
    span.set_attribute("checkpoint.blob_cache_hits", cache_hits)
    span.set_attribute("checkpoint.blob_cache_hit_bytes", cache_hit_bytes)
    span.set_attribute("checkpoint.blob_fetched_bytes", fetched_bytes)


def _raw_blob_triples(raw: object) -> list[tuple[bytes, bytes, bytes | None]]:
    if not is_object_list(raw):
        return []
    triples: list[tuple[bytes, bytes, bytes | None]] = []
    for item in raw:
        if (
            is_object_list(item)
            and len(item) == 3
            and isinstance(item[0], bytes)
            and isinstance(item[1], bytes)
            and (item[2] is None or isinstance(item[2], bytes))
        ):
            triples.append((item[0], item[1], item[2]))
    return triples


@dataclass(frozen=True)
class BlobCacheStats:
    hits: int
    fetched_blobs: int
    total_bytes: int


# meiva:note
# Byte-bounded LRU cache for immutable `checkpoint_blobs` rows.
#
# Immutability rests on two preconditions:
# 1. Postgres `checkpoint_blobs` has PRIMARY KEY `(thread_id, checkpoint_ns, channel, version)`
#    and is only ever written with `ON CONFLICT DO NOTHING`, so an existing row is never mutated.
# 2. A `thread_id` never reuses a previously deleted `version` string: LangGraph's
#    `get_next_version` appends a random suffix, agent IDs are never reused, forks
#    (`checkpoint_copy.py`) write to a new `thread_id`, and `blob_vacuum` only reclaims dead
#    tuples without deleting rows. Bypassing this saver with raw SQL delete + same-version
#    reinsert would violate precondition (2) and serve a stale cached blob.
#
# Indexing by `(thread_id, checkpoint_ns, channel)` keeps only the latest observed version per
# channel so the exclusion array passed to Postgres stays bounded to the thread's channel count
# (~20 entries) and total memory stays under `max_bytes`.
class _CheckpointBlobCache:
    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max(0, max_bytes)
        self._entries: OrderedDict[tuple[str, str, str], tuple[str, bytes, bytes]] = OrderedDict()
        self._by_scope: dict[tuple[str, str], set[str]] = {}
        self._total_bytes = 0
        self.hits = 0
        self.fetched_blobs = 0

    @property
    def enabled(self) -> bool:
        return self._max_bytes > 0

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def stats(self) -> BlobCacheStats:
        return BlobCacheStats(
            hits=self.hits,
            fetched_blobs=self.fetched_blobs,
            total_bytes=self._total_bytes,
        )

    def snapshot_for(
        self, thread_id: str, checkpoint_ns: str
    ) -> dict[str, tuple[str, bytes, bytes]]:
        channels = self._by_scope.get((thread_id, checkpoint_ns))
        if not channels:
            return {}
        out: dict[str, tuple[str, bytes, bytes]] = {}
        for channel in channels:
            entry = self._entries.get((thread_id, checkpoint_ns, channel))
            if entry is not None:
                out[channel] = entry
        return out

    def touch(self, thread_id: str, checkpoint_ns: str, channel: str) -> None:
        key = (thread_id, checkpoint_ns, channel)
        if key in self._entries:
            self._entries.move_to_end(key)

    def put(
        self,
        thread_id: str,
        checkpoint_ns: str,
        channel: str,
        version: str,
        blob_type: bytes,
        blob_bytes: bytes,
    ) -> None:
        if self._max_bytes <= 0 or blob_type == b"empty":
            return
        size = len(blob_bytes)
        if size > self._max_bytes:
            return
        key = (thread_id, checkpoint_ns, channel)
        prev = self._entries.pop(key, None)
        if prev is not None:
            self._total_bytes -= len(prev[2])
        while self._entries and self._total_bytes + size > self._max_bytes:
            evicted_key, evicted_val = self._entries.popitem(last=False)
            self._total_bytes -= len(evicted_val[2])
            scope_key = (evicted_key[0], evicted_key[1])
            scope_channels = self._by_scope.get(scope_key)
            if scope_channels is not None:
                scope_channels.discard(evicted_key[2])
                if not scope_channels:
                    del self._by_scope[scope_key]
        self._entries[key] = (version, blob_type, blob_bytes)
        self._by_scope.setdefault((thread_id, checkpoint_ns), set()).add(channel)
        self._total_bytes += size

    def drop_thread(self, thread_id: str) -> None:
        scopes = [scope for scope in self._by_scope if scope[0] == thread_id]
        for scope in scopes:
            channels = self._by_scope.pop(scope, ())
            for channel in channels:
                prev = self._entries.pop((scope[0], scope[1], channel), None)
                if prev is not None:
                    self._total_bytes -= len(prev[2])


def _is_dict_row_pool(
    _pool: AsyncConnectionPool[AsyncConnection],
) -> TypeGuard[AsyncConnectionPool[AsyncConnection[DictRow]]]:
    return True


@dataclass(frozen=True)
class _OwnerRow:
    machine: str
    runtime_owner: UUID | None
    status: str


def _thread_id(config: RunnableConfig) -> str:
    configurable = config.get("configurable", {})
    thread_id = object_of(configurable.get, "thread_id", "")
    if not isinstance(thread_id, str):
        raise TypeError("the checkpoint config's thread_id must be a string")
    return thread_id


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
        blob_cache_max_bytes: int | None = None,
    ) -> None:
        super().__init__(conn=conn, serde=serde)
        self._pool = conn
        self._expected_owner = expected_owner
        self._machine = machine
        cap = (
            _DEFAULT_BLOB_CACHE_MAX_BYTES if blob_cache_max_bytes is None else blob_cache_max_bytes
        )
        self._blob_cache = _CheckpointBlobCache(cap)

    @classmethod
    def from_pool(
        cls,
        pool: AsyncConnectionPool[AsyncConnection],
        *,
        serde: SerializerProtocol | None = None,
        expected_owner: UUID | None = None,
        machine: str | None = None,
        blob_cache_max_bytes: int | None = None,
    ) -> "PooledPostgresSaver":
        if not _is_dict_row_pool(pool):
            raise TypeError("expected DictRow pool")
        return cls(
            conn=pool,
            serde=serde,
            expected_owner=expected_owner,
            machine=machine,
            blob_cache_max_bytes=blob_cache_max_bytes,
        )

    def _merge_cached_blobs(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str,
        channel_versions: Mapping[str, object],
        raw_fetched: Sequence[tuple[bytes, bytes, bytes | None]],
        cached: Mapping[str, tuple[str, bytes, bytes]],
    ) -> tuple[list[tuple[bytes, bytes, bytes]], int, int, int]:
        merged: list[tuple[bytes, bytes, bytes]] = []
        fetched_channels: set[str] = set()
        fetched_bytes = 0
        for ch_b, type_b, blob_b in raw_fetched:
            ch = ch_b.decode()
            fetched_channels.add(ch)
            if blob_b is not None:
                merged.append((ch_b, type_b, blob_b))
                fetched_bytes += len(blob_b)
                ver = channel_versions.get(ch)
                if isinstance(ver, str) and type_b != b"empty":
                    self._blob_cache.put(
                        thread_id, checkpoint_ns, ch, ver, bytes(type_b), bytes(blob_b)
                    )

        cache_hits = 0
        cache_hit_bytes = 0
        if cached:
            for ch, ver in channel_versions.items():
                if ch in fetched_channels or not isinstance(ver, str):
                    continue
                entry = cached.get(ch)
                if entry is not None and entry[0] == ver:
                    merged.append((ch.encode(), entry[1], entry[2]))
                    self._blob_cache.touch(thread_id, checkpoint_ns, ch)
                    cache_hits += 1
                    cache_hit_bytes += len(entry[2])
        self._blob_cache.hits += cache_hits
        self._blob_cache.fetched_blobs += len(fetched_channels)
        return merged, cache_hits, cache_hit_bytes, fetched_bytes

    def blob_cache_stats(self) -> BlobCacheStats:
        return self._blob_cache.stats()

    async def _fetch_checkpoint_row(
        self, query: LiteralString, params: tuple[object, ...], thread_id: str
    ) -> tuple[DictRow | None, list[tuple[bytes, bytes]]]:
        async with self._cursor() as cur:
            _ = await cur.execute(query, params, binary=True)
            value = await cur.fetchone()
            if value is None:
                return None, []
            parent_id = _needs_legacy_sends_migration(object_mapping(value))
            if parent_id is None:
                return value, []
            _ = await cur.execute(self.SELECT_PENDING_SENDS_SQL, (thread_id, [parent_id]))
            sends_row = await cur.fetchone()
            if sends_row is None:
                return value, []
            return value, _pending_sends_pairs(object_mapping(sends_row).get("sends"))

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        if (
            not self._blob_cache.enabled
            or type(self).SELECT_SQL is not BasePostgresSaver.SELECT_SQL
        ):
            return await super().aget_tuple(config)

        configurable = object_mapping(config.get("configurable") or {})
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        cached = self._blob_cache.snapshot_for(thread_id, checkpoint_ns)
        query, params = _tuple_query(thread_id, checkpoint_ns, get_checkpoint_id(config), cached)

        with otel_trace.get_tracer(__name__).start_as_current_span("checkpoint.aget_tuple") as span:
            t_sql0 = time.monotonic()
            value, pending_sends = await self._fetch_checkpoint_row(query, params, thread_id)
            if value is None:
                return None
            sql_ms = (time.monotonic() - t_sql0) * 1000.0
            row_map = object_mapping(value)
            merged, cache_hits, cache_hit_bytes, fetched_bytes = self._merge_cached_blobs(
                thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
                channel_versions=_channel_versions_of(row_map),
                raw_fetched=_raw_blob_triples(row_map.get("channel_values")),
                cached=cached,
            )
            raw_cp = row_map.get("checkpoint")
            if pending_sends and is_object_dict(raw_cp):
                self._migrate_pending_sends(pending_sends, raw_cp, merged)
            t_dec0 = time.monotonic()
            decoded_blobs = await asyncio.to_thread(self._load_blobs, merged) if merged else {}
            value["channel_values"] = []
            loaded = await super()._load_checkpoint_tuple(value)
            loaded.checkpoint["channel_values"].update(decoded_blobs)
            _record_tuple_span(
                span,
                thread_id=thread_id,
                sql_ms=sql_ms,
                decode_ms=(time.monotonic() - t_dec0) * 1000.0,
                cache_hits=cache_hits,
                cache_hit_bytes=cache_hit_bytes,
                fetched_bytes=fetched_bytes,
            )
            return loaded

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

    # meiva:link services/docs/agent_runner_side/checkpoint-writes.md
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

        row = await aquery_one(
            conn,
            _OwnerRow,
            "SELECT machine, runtime_owner, status FROM agents_meta WHERE id = %s FOR SHARE",
            (agent_id,),
        )
        if row is None:
            return

        _check_row_ownership(
            agent_id,
            row.machine,
            row.runtime_owner,
            row.status,
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
        thread_id = _thread_id(config)
        parent = configurable.get("checkpoint_id")
        async with self._pool.connection() as conn, conn.transaction():
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
            saved_config = await lease.aput(config, checkpoint, metadata, new_versions)
            await self._verify_owner(conn, thread_id)
            return saved_config

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, object]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = _thread_id(config)
        async with self._pool.connection() as conn, conn.transaction():
            lease = AsyncPostgresSaver(conn=conn, serde=self.serde)
            await lease.aput_writes(config, writes, task_id, task_path)
            await self._verify_owner(conn, thread_id)

    async def adelete_thread(self, thread_id: str) -> None:
        self._blob_cache.drop_thread(thread_id)
        async with self._pool.connection() as conn, conn.transaction():
            await self._verify_owner(conn, thread_id, for_delete=True)
            lease = AsyncPostgresSaver(conn=conn, serde=self.serde)
            await lease.adelete_thread(thread_id)
        self._blob_cache.drop_thread(thread_id)

    @asynccontextmanager
    async def _cursor(self, *, pipeline: bool = False) -> AsyncGenerator[AsyncCursor[DictRow]]:
        async with self._pool.connection() as conn:
            lease = AsyncPostgresSaver(conn=conn, serde=self.serde)
            async with lease._cursor(pipeline=pipeline) as cur:
                yield cur
