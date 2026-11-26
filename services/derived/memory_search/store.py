"""Serialize cache writers and publish one immutable snapshot after each save."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from threading import Lock

import numpy as np
from numpy.typing import NDArray

from services.derived.memory_indexer.backends.base import KIND_BODY, ChunkHit, pk_of
from services.derived.memory_search.cache import load_snapshot, save_snapshot, write_npz
from services.derived.memory_search.snapshot import Row, Snapshot

_log = logging.getLogger("services.derived.memory_search.store")


class MemoryStore:
    def __init__(
        self,
        data_file: Path,
        dim: int,
        fingerprint: str,
        *,
        replace_file: Callable[[Path, Path], object] = Path.replace,
        write_archive: Callable[[Path, Snapshot], None] = write_npz,
    ) -> None:
        self._data_file = data_file
        self._dim = dim
        self._replace_file = replace_file
        self._write_archive = write_archive
        self._writer = Lock()
        self._snapshot = Snapshot.build((), np.empty((0, dim), dtype=np.float32), fingerprint)

    def require_space(self, fingerprint: str | None) -> None:
        if fingerprint != self._snapshot.fingerprint:
            raise ValueError(
                "embedding space mismatch; restart all memory services with matching configuration"
            )

    def load(self) -> None:
        with self._writer:
            snapshot = load_snapshot(self._data_file, self._dim, self._snapshot.fingerprint)
            if snapshot is not None:
                self._snapshot = snapshot
                _log.info(
                    "[memory_search] loaded %d rows from %s", len(snapshot.rows), self._data_file
                )

    def _commit(self, rows: tuple[Row, ...], matrix: NDArray[np.float32]) -> None:
        """Called with the writer lock held. Publish only after the rename succeeds."""
        current = self._snapshot
        candidate = Snapshot.build(rows, matrix, current.fingerprint, saves=current.saves + 1)
        elapsed = save_snapshot(self._data_file, candidate, self._replace_file, self._write_archive)
        self._snapshot = replace(candidate, last_save_seconds=elapsed)

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
        self.upsert_many([(path, mtime, content_hash, embedding, kind, chunk_idx)])

    def upsert_many(self, rows: Sequence[tuple[str, float, str, np.ndarray, str, int]]) -> None:
        """Commit one batch, preserving row order and letting the last duplicate win."""
        updates: dict[str, tuple[Row, NDArray[np.float32]]] = {}
        for path, mtime, content_hash, embedding, kind, chunk_idx in rows:
            vector = embedding.astype(np.float32).reshape(-1)
            if vector.shape != (self._dim,):
                raise ValueError(f"embedding shape {vector.shape} != ({self._dim},)")
            pk = pk_of(path, kind, chunk_idx)
            updates[pk] = (Row(pk, path, kind, int(chunk_idx), float(mtime), content_hash), vector)
        if not updates:
            return

        with self._writer:
            current = self._snapshot
            metadata = list(current.rows)
            existing = {row.pk: index for index, row in enumerate(metadata)}
            row_count = len(metadata) + len(updates.keys() - existing.keys())
            matrix = np.empty((row_count, self._dim), dtype=np.float32)
            matrix[: len(metadata)] = current.matrix
            for pk, (row, vector) in updates.items():
                index = existing.get(pk)
                if index is None:
                    index = len(metadata)
                    metadata.append(row)
                else:
                    metadata[index] = row
                matrix[index] = vector
            self._commit(tuple(metadata), matrix)

    def _delete_where(self, remove: Callable[[Row], bool]) -> None:
        with self._writer:
            current = self._snapshot
            keep = [index for index, row in enumerate(current.rows) if not remove(row)]
            if len(keep) == len(current.rows):
                self._commit(current.rows, current.matrix)
            else:
                self._commit(tuple(current.rows[index] for index in keep), current.matrix[keep])

    def delete(self, path: str) -> None:
        """Delete every chunk of the path."""
        self._delete_where(lambda row: row.path == path)

    def delete_stale_rows(self, entries: Sequence[tuple[str, dict[str, int]]]) -> None:
        """Delete tails and kinds the current files no longer produce."""
        limits = dict(entries)
        self._delete_where(
            lambda row: row.path in limits and row.chunk_idx >= limits[row.path].get(row.kind, 0)
        )

    def all_meta(self) -> dict[str, tuple[float, str, str]]:
        return self._snapshot.all_meta()

    def search_topk(self, query_vector: np.ndarray, k: int) -> list[ChunkHit]:
        return self._snapshot.search_topk(query_vector, k)

    def stats(self) -> tuple[int, float | None]:
        snapshot = self._snapshot
        return len(snapshot.rows), snapshot.last_save_seconds

    @property
    def last_save_seconds(self) -> float | None:
        return self._snapshot.last_save_seconds

    @property
    def saves(self) -> int:
        return self._snapshot.saves

    def __len__(self) -> int:
        return len(self._snapshot.rows)
