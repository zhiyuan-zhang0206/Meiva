"""The NumPy store — chunk rows in memory + npz persistence.

One `MemoryStore` instance lives for the service process's lifetime. All
rows sit in Python lists + one float32 matrix; mutations rewrite the npz
atomically (tmp file + `os.replace`), so a crash mid-save can never leave
a torn file behind. The service wraps every operation in one asyncio
lock — search included (a full exact scan is microseconds at this scale,
so the lock is never contended meaningfully).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple, TypeGuard
from zipfile import BadZipFile

import numpy as np
from numpy.lib.npyio import NpzFile
from numpy.typing import NDArray

from base.host.boundary.json_values import is_object_list
from base.host.boundary.untyped import object_of
from services.derived.memory_indexer.backends.base import KIND_BODY, ChunkHit, pk_of

_log = logging.getLogger("services.derived.memory_search.store")

_NPZ_VECTORS = "vectors"
_NPZ_PKS = "pks"
_NPZ_PATHS = "paths"
_NPZ_KINDS = "kinds"
_NPZ_CHUNK_IDX = "chunk_idx"
_NPZ_MTIMES = "mtimes"
_NPZ_HASHES = "content_hashes"
_NPZ_EMBEDDERS = "embedders"

_NPZ_KEYS = frozenset(
    {
        _NPZ_VECTORS,
        _NPZ_PKS,
        _NPZ_PATHS,
        _NPZ_KINDS,
        _NPZ_CHUNK_IDX,
        _NPZ_MTIMES,
        _NPZ_HASHES,
        _NPZ_EMBEDDERS,
    }
)


# meiva:note
# `np.load` types its answer `Any`; an opened npz archive is a mapping of named arrays.
def _is_npz(value: object) -> TypeGuard[NpzFile[np.generic]]:
    return isinstance(value, NpzFile)


# meiva:note
# The items of a 1-D array as python values, still `object` until a reader narrows each.
def _items(array: NDArray[np.generic]) -> list[object]:
    items = object_of(array.tolist)
    if not is_object_list(items):
        raise ValueError("a 1-D array lists its items")
    return items


def _number(value: object) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    raise ValueError("an array item is not a number")


# meiva:note
# The columns of a cache archive, each converted to its Python type; a column that does not convert
# raises ValueError, so the caller commits all of them or none.
class _Columns(NamedTuple):
    pks: list[str]
    paths: list[str]
    kinds: list[str]
    chunk_idx: list[int]
    mtimes: list[float]
    hashes: list[str]
    matrix: NDArray[np.float32]
    embedders: list[str]


def _columns_of(data: NpzFile[np.generic]) -> _Columns:
    matrix = data[_NPZ_VECTORS].astype(np.float32)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    return _Columns(
        pks=[str(p) for p in _items(data[_NPZ_PKS])],
        paths=[str(p) for p in _items(data[_NPZ_PATHS])],
        kinds=[str(k) for k in _items(data[_NPZ_KINDS])],
        chunk_idx=[int(_number(i)) for i in _items(data[_NPZ_CHUNK_IDX])],
        mtimes=[_number(m) for m in _items(data[_NPZ_MTIMES])],
        hashes=[str(h) for h in _items(data[_NPZ_HASHES])],
        matrix=matrix,
        embedders=[str(e) for e in _items(data[_NPZ_EMBEDDERS])],
    )


class MemoryStore:
    def __init__(self, data_file: Path, dim: int, fingerprint: str) -> None:
        self._data_file = data_file
        self._dim = dim
        self._fingerprint = fingerprint
        self._pk_index: dict[str, int] = {}
        self._pks: list[str] = []
        self._paths: list[str] = []
        self._kinds: list[str] = []
        self._chunk_idx: list[int] = []
        self._mtimes: list[float] = []
        self._hashes: list[str] = []
        self._embedders: list[str] = []
        self._matrix = np.empty((0, dim), dtype=np.float32)
        # Duration of the most recent successful save(), None until the first
        # one — the /stats endpoint and the 60s stats flusher expose it so a
        # growing npz cost is visible before it becomes a write stall.
        self._last_save_seconds: float | None = None
        self._saves = 0

    def require_space(self, fingerprint: str | None) -> None:
        # meiva:note
        # Reject a client from another embedding generation before reading or writing.
        if fingerprint != self._fingerprint:
            raise ValueError(
                "embedding space mismatch; restart all memory services with matching configuration"
            )

    # meiva:note
    # Matching width alone is insufficient: every cached row must name the configured space.
    def _cache_matches_space(self, matrix: np.ndarray, embedders: Sequence[str]) -> bool:
        return (
            matrix.ndim == 2
            and matrix.shape[1] == self._dim
            and len(embedders) == matrix.shape[0]
            and all(fingerprint == self._fingerprint for fingerprint in embedders)
        )

    # ── persistence ──────────────────────────────────────────────────────

    def load(self) -> None:
        if not self._data_file.exists():
            return
        try:
            opened = object_of(np.load, self._data_file)
            if not _is_npz(opened):
                _log.warning(
                    "[memory_search] %s is not an npz archive — starting empty", self._data_file
                )
                return
            with opened as data:
                missing = _NPZ_KEYS - set(data.files)
                if missing:
                    # A torn/foreign file is a broken cache — start empty and let
                    _log.warning(
                        "[memory_search] %s missing keys %s — starting empty; "
                        "cold-start will rebuild the index",
                        self._data_file,
                        sorted(missing),
                    )
                    return
                columns = _columns_of(data)
            if not self._cache_matches_space(columns.matrix, columns.embedders):
                _log.warning(
                    "[memory_search] %s cache has an incompatible vector space — starting empty",
                    self._data_file,
                )
                return
        except (OSError, ValueError, EOFError, KeyError, BadZipFile) as exc:
            # np.load raises ValueError / EOFError on foreign bytes, BadZipFile
            # (a plain Exception subclass, not OSError) on a truncated zip,
            # OSError on read failures, a malformed array can surface a
            # KeyError on access, and a column that fails conversion raises
            # ValueError — every one of them is the same "broken cache"
            # condition.
            _log.warning(
                "[memory_search] %s does not parse or fully convert (%s: %s) — "
                "starting empty; cold-start will rebuild the index",
                self._data_file,
                type(exc).__name__,
                exc,
            )
            return
        # Every column converted — commit all-or-nothing, so a mid-file
        # failure can never leave a half-loaded store (a few columns
        # populated, the rest empty).
        self._pks = columns.pks
        self._paths = columns.paths
        self._kinds = columns.kinds
        self._chunk_idx = columns.chunk_idx
        self._mtimes = columns.mtimes
        self._hashes = columns.hashes
        self._embedders = columns.embedders
        self._matrix = columns.matrix
        self._pk_index = {pk: idx for idx, pk in enumerate(columns.pks)}
        _log.info("[memory_search] loaded %d rows from %s", len(self._pks), self._data_file)

    def save(self) -> None:
        """Atomically rewrite the npz — tmp file in the same directory +
        `os.replace` (same-filesystem rename is atomic).

        Times the whole write+replace and records it as
        `last_save_seconds` on success only — a failed save (np.savez
        raising) leaves the previous duration untouched, because the
        number operators read is "what did the last completed persistence
        cost", not "what did the last attempt cost"."""
        start = time.perf_counter()
        self._data_file.parent.mkdir(parents=True, exist_ok=True)
        # np.savez appends ".npz" to any name not already ending in it, so the
        # tmp name must end in ".npz" for the replace below to find the file.
        tmp = self._data_file.with_suffix(".tmp.npz")
        np.savez(
            tmp,
            pks=np.array(self._pks, dtype=str),
            paths=np.array(self._paths, dtype=str),
            kinds=np.array(self._kinds, dtype=str),
            chunk_idx=np.array(self._chunk_idx, dtype=np.int64),
            mtimes=np.array(self._mtimes, dtype=np.float64),
            content_hashes=np.array(self._hashes, dtype=str),
            embedders=np.array(self._embedders, dtype=str),
            vectors=self._matrix,
        )
        tmp.replace(self._data_file)
        self._last_save_seconds = time.perf_counter() - start
        self._saves += 1

    # ── mutations ────────────────────────────────────────────────────────

    def upsert(
        self,
        path: str,
        mtime: float,
        content_hash: str,
        embedding: np.ndarray,
        *,
        kind: str = KIND_BODY,
        chunk_idx: int = 0,
    ) -> None:
        """Write / update one chunk row; re-upserting the same triple
        overwrites in place."""
        self.upsert_many([(path, mtime, content_hash, embedding, kind, chunk_idx)])

    def upsert_many(self, rows: Sequence[tuple[str, float, str, np.ndarray, str, int]]) -> None:
        """Apply chunk rows in order without persisting.

        Every vector is normalized and dimension-checked before any state
        changes. New rows grow the matrix together, while repeated keys update
        the row reserved by their first occurrence so the last write wins.
        """
        validated_rows: list[tuple[str, float, str, np.ndarray, str, int]] = []
        for path, mtime, content_hash, embedding, kind, chunk_idx in rows:
            vector = embedding.astype(np.float32).reshape(-1)
            if vector.shape != (self._dim,):
                raise ValueError(f"embedding shape {vector.shape} != ({self._dim},)")
            validated_rows.append((path, float(mtime), content_hash, vector, kind, int(chunk_idx)))

        initial_row_count = len(self._pks)
        new_vectors: list[np.ndarray] = []
        for path, mtime, content_hash, vector, kind, chunk_idx in validated_rows:
            pk = pk_of(path, kind, chunk_idx)
            idx = self._pk_index.get(pk)
            if idx is None:
                idx = len(self._pks)
                self._pks.append(pk)
                self._paths.append(path)
                self._kinds.append(kind)
                self._chunk_idx.append(chunk_idx)
                self._mtimes.append(mtime)
                self._hashes.append(content_hash)
                self._embedders.append(self._fingerprint)
                self._pk_index[pk] = idx
                new_vectors.append(vector)
                continue

            self._paths[idx] = path
            self._kinds[idx] = kind
            self._chunk_idx[idx] = chunk_idx
            self._mtimes[idx] = mtime
            self._hashes[idx] = content_hash
            self._embedders[idx] = self._fingerprint
            if idx < initial_row_count:
                self._matrix[idx] = vector
            else:
                new_vectors[idx - initial_row_count] = vector

        if new_vectors:
            self._matrix = np.concatenate((self._matrix, new_vectors))

    def delete(self, path: str) -> None:
        """Delete every chunk row of `path`; no-op when the path is absent."""
        keep_idx = [i for i, p in enumerate(self._paths) if p != path]
        if len(keep_idx) == len(self._paths):
            return
        self._pks = [self._pks[i] for i in keep_idx]
        self._paths = [self._paths[i] for i in keep_idx]
        self._kinds = [self._kinds[i] for i in keep_idx]
        self._chunk_idx = [self._chunk_idx[i] for i in keep_idx]
        self._mtimes = [self._mtimes[i] for i in keep_idx]
        self._hashes = [self._hashes[i] for i in keep_idx]
        self._embedders = [self._embedders[i] for i in keep_idx]
        self._matrix = self._matrix[keep_idx]
        self._pk_index = {pk: idx for idx, pk in enumerate(self._pks)}

    def delete_stale_rows(
        self,
        entries: Sequence[tuple[str, dict[str, int]]],
    ) -> None:
        """Delete tail rows the current files no longer produce (issue #1946).

        See the backend protocol: `kind_limits` maps kind -> the current row
        count; rows of the path whose kind is absent or whose chunk_idx >=
        its limit are removed. A path with no rows is a no-op.
        """
        limits_by_path = dict(entries)
        keep_idx = [
            i
            for i, (p, kind, idx) in enumerate(
                zip(self._paths, self._kinds, self._chunk_idx, strict=True)
            )
            if p not in limits_by_path or idx < limits_by_path[p].get(kind, 0)
        ]
        if len(keep_idx) == len(self._paths):
            return
        self._pks = [self._pks[i] for i in keep_idx]
        self._paths = [self._paths[i] for i in keep_idx]
        self._kinds = [self._kinds[i] for i in keep_idx]
        self._chunk_idx = [self._chunk_idx[i] for i in keep_idx]
        self._mtimes = [self._mtimes[i] for i in keep_idx]
        self._hashes = [self._hashes[i] for i in keep_idx]
        self._embedders = [self._embedders[i] for i in keep_idx]
        self._matrix = self._matrix[keep_idx]
        self._pk_index = {pk: idx for idx, pk in enumerate(self._pks)}

    def all_meta(self) -> dict[str, tuple[float, str, str]]:
        """Per-path (mtime, content_hash, provider_fingerprint) — one entry
        per **file**, not per chunk (aggregation keeps the max mtime). The fingerprint is the reconcile key's third
        element."""
        meta: dict[str, tuple[float, str, str]] = {}
        for path, mtime, hash_, fingerprint in zip(
            self._paths, self._mtimes, self._hashes, self._embedders, strict=True
        ):
            prev = meta.get(path)
            if prev is None or mtime > prev[0]:
                meta[path] = (mtime, hash_, fingerprint)
        return meta

    def search_topk(self, query_vector: np.ndarray, k: int) -> list[ChunkHit]:
        """Exact cosine top-k **files**, each with its best chunk row.

        One matrix product over every row (microseconds at pool scale), then
        per path keep the best (maximum) cosine row — most-similar-first.
        Returns fewer than k when the store has fewer distinct paths; empty
        when the store is empty.
        """
        query = query_vector.astype(np.float32).reshape(-1)
        if query.shape != (self._dim,):
            raise ValueError(f"query shape {query.shape} != ({self._dim},)")
        if self._matrix.shape[0] == 0:
            return []
        q_norm = float(np.linalg.norm(query))
        if q_norm == 0.0:
            # Degenerate query: every similarity is 0; keep each path's first row, stable order.
            first: dict[str, int] = {}
            for row, path in enumerate(self._paths):
                first.setdefault(path, row)
            return [self._hit(row, 0.0) for row in list(first.values())[:k]]
        row_norms = np.linalg.norm(self._matrix, axis=1)
        scores = [_number(c) for c in _items(self._matrix @ query / (row_norms * q_norm))]
        best: dict[str, int] = {}
        for row, path in enumerate(self._paths):
            if path not in best or scores[row] > scores[best[path]]:
                best[path] = row
        ranked = sorted(best.values(), key=lambda row: scores[row], reverse=True)[:k]
        return [self._hit(row, scores[row]) for row in ranked]

    def _hit(self, row: int, score: float) -> ChunkHit:
        return ChunkHit(self._paths[row], self._kinds[row], self._chunk_idx[row], score)

    @property
    def last_save_seconds(self) -> float | None:
        """Duration (seconds) of the most recent successful save — None until
        the first save since process start."""
        return self._last_save_seconds

    @property
    def saves(self) -> int:
        return self._saves

    def __len__(self) -> int:
        return len(self._pks)
