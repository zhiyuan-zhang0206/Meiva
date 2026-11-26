"""The memory search HTTP API — FastAPI over the in-process MemoryStore.

Endpoints mirror the backend protocol one-to-one (upsert / upsert_batch /
delete / meta / search), so `backends.numpy.NumPyBackend` is a thin HTTP client (the indexer daemon
and the gateway talk to it over HTTP). Every mutation persists the npz before responding, so a kill
-after-ack never loses a row.

`GET /stats` is the one non-protocol endpoint: current chunk rows plus the
duration of the most recent npz save — the ops surface for the memory-search
row-growth monitoring. A lifespan background task samples the same two
numbers every 60s and emits them as a `memory_search_stats` telemetry
event, so they reach Prometheus through the existing OTLP path under this
process's own job label (the `service_started` series rides the same path).

Binds loopback only (the daemon passes host="127.0.0.1") — a strictly local
service; no LAN port is opened.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import numpy as np
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from base import telemetry
from services.derived.memory_indexer.backends.base import EMBEDDING_SPACE_HEADER, INDEX_API_PREFIX
from services.derived.memory_search.store import MemoryStore

_log = logging.getLogger("services.derived.memory_search.app")

# Loopback-only is the trust model, not a security boundary. Each app's
# composition root supplies its embedding width so the wire models reject
# oversized vectors before numpy allocates; the store also checks exact width.
MAX_K = 1000

# One stats sample per minute — bounded row rate, same cadence as the
# gateway's gauge flushers (agent_registry / auth401 / latency). A 60s
# gauge is plenty for a growth curve that moves with note churn.
_STATS_FLUSH_INTERVAL_S = 60.0


class DeleteBody(BaseModel):
    path: str


class DeleteStaleRow(BaseModel):
    path: str
    kind_limits: dict[str, int]


class DeleteStaleBatchBody(BaseModel):
    entries: list[DeleteStaleRow] = Field(min_length=1)


def emit_memory_search_stats(rows: int, last_save_seconds: float | None) -> None:
    """Emit one `memory_search_stats` telemetry event carrying the store's
    absolute state.

    `rows` and `last_save_seconds` are state, never sums — the
    `_METRIC_DISPOSITION` override records both as ObservableGauges
    (`meiva_memory_search_stats_rows_ratio` /
    `meiva_memory_search_stats_last_save_seconds`), so a flat store does not
    accrue value the way Counters would. `last_save_seconds` is None until
    the first save since boot; the field is omitted then (an absent optional
    metric is not zero).

    Exposed separately from the flusher so tests can drive it directly
    (mirrors `services/upkeep/events_maintenance/registry_gauge.py:emit_max_agent_id`).
    """
    attributes: dict[str, int | float] = {"rows": rows}
    if last_save_seconds is not None:
        attributes["last_save_seconds"] = last_save_seconds
    telemetry.emit("telemetry", "memory_search_stats", attributes=attributes)


async def _stats_flusher(store: MemoryStore) -> None:
    """Emit periodic stats from a single committed snapshot."""
    while True:
        await asyncio.sleep(_STATS_FLUSH_INTERVAL_S)
        try:
            emit_memory_search_stats(*store.stats())
        except Exception:
            _log.warning("[memory_search] stats emit failed", exc_info=True)


def _mount_mutations(
    app: APIRouter, store: MemoryStore, max_batch_rows: int, embedding_dim: int
) -> None:
    """The four protocol write endpoints (upsert / upsert_batch / delete /
    delete_stale_batch) — extracted so build_app stays a router, not a wall
    of handlers. Each mutation persists the npz before responding."""
    writer_admission = asyncio.Lock()

    class UpsertBody(BaseModel):
        path: str
        mtime: float
        content_hash: str
        kind: str
        chunk_idx: int
        vector: list[float] = Field(max_length=embedding_dim)

    class UpsertBatchBody(BaseModel):
        rows: list[UpsertBody] = Field(min_length=1)

    def check_batch(size: int) -> None:
        if size > max_batch_rows:
            raise HTTPException(
                status_code=422, detail=f"batch of {size} rows exceeds the limit {max_batch_rows}"
            )

    def upsert_rows(rows: list[UpsertBody]) -> None:
        store.upsert_many(
            [
                (
                    row.path,
                    row.mtime,
                    row.content_hash,
                    np.asarray(row.vector, dtype=np.float32),
                    row.kind,
                    row.chunk_idx,
                )
                for row in rows
            ]
        )

    async def commit_upserts(rows: list[UpsertBody]) -> dict[str, str]:
        try:
            async with writer_admission:
                await asyncio.to_thread(upsert_rows, rows)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "ok"}

    @app.post("/upsert")
    async def upsert(body: UpsertBody) -> dict[str, str]:
        return await commit_upserts([body])

    @app.post("/upsert_batch")
    async def upsert_batch(body: UpsertBatchBody) -> dict[str, str]:
        check_batch(len(body.rows))
        return await commit_upserts(body.rows)

    @app.post("/delete")
    async def delete(body: DeleteBody) -> dict[str, str]:
        async with writer_admission:
            await asyncio.to_thread(store.delete, body.path)
        return {"status": "ok"}

    @app.post("/delete_stale_batch")
    async def delete_stale_batch(body: DeleteStaleBatchBody) -> dict[str, str]:
        """Persist tail cleanup for all supplied paths in one npz save."""
        check_batch(len(body.entries))
        async with writer_admission:
            await asyncio.to_thread(
                store.delete_stale_rows, [(row.path, row.kind_limits) for row in body.entries]
            )
        return {"status": "ok"}


def build_app(store: MemoryStore, max_batch_rows: int, *, embedding_dim: int) -> FastAPI:
    """Serve committed snapshots while writers build and persist the next one."""

    class SearchBody(BaseModel):
        vector: list[float] = Field(max_length=embedding_dim)
        k: int = Field(ge=1, le=MAX_K)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        async with asyncio.TaskGroup() as background:
            flusher = background.create_task(_stats_flusher(store))
            try:
                yield
            finally:
                _ = flusher.cancel()

    app = FastAPI(title="meiva-memory-search", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/stats")
    async def stats() -> dict[str, int | float | None]:
        rows, elapsed = store.stats()
        return {"rows": rows, "last_save_seconds": elapsed}

    async def require_space(request: Request) -> None:
        try:
            store.require_space(request.headers.get(EMBEDDING_SPACE_HEADER))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    protocol = APIRouter(prefix=INDEX_API_PREFIX, dependencies=[Depends(require_space)])
    _mount_mutations(protocol, store, max_batch_rows, embedding_dim)

    @protocol.get("/meta")
    def meta() -> dict[str, tuple[float, str, str]]:
        """Per-path (mtime, content_hash, provider_fingerprint) — same shape
        the backend protocol's `all_meta` returns (the reconcile key)."""
        return store.all_meta()

    @protocol.post("/search")
    def search(body: SearchBody) -> dict[str, list[dict[str, str | int | float]]]:
        vector = np.asarray(body.vector, dtype=np.float32)
        try:
            hits = store.search_topk(vector, body.k)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"hits": [hit._asdict() for hit in hits]}

    app.include_router(protocol)
    return app
