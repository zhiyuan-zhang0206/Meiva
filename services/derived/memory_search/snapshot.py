# meiva:link services/docs/gateway_side/memory_indexer/memory_indexer.md

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

import numpy as np
from numpy.typing import NDArray

from base.host.boundary.json_values import is_object_list
from base.host.boundary.untyped import object_of
from services.derived.memory_indexer.backends.base import ChunkHit


def array_items(array: NDArray[np.generic]) -> list[object]:
    items = object_of(array.tolist)
    if not is_object_list(items):
        raise ValueError("a 1-D array lists its items")
    return items


def array_number(value: object) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    raise ValueError("an array item is not a number")


class Row(NamedTuple):
    pk: str
    path: str
    kind: str
    chunk_idx: int
    mtime: float
    content_hash: str


@dataclass(frozen=True, slots=True)
class Snapshot:
    rows: tuple[Row, ...]
    matrix: np.ndarray[tuple[int, int], np.dtype[np.float32]]
    fingerprint: str
    last_save_seconds: float | None = None
    saves: int = 0

    def __post_init__(self) -> None:
        self.matrix.flags.writeable = False

    def all_meta(self) -> dict[str, tuple[float, str, str]]:
        meta: dict[str, tuple[float, str, str]] = {}
        for row in self.rows:
            prev = meta.get(row.path)
            if prev is None or row.mtime > prev[0]:
                meta[row.path] = (row.mtime, row.content_hash, self.fingerprint)
        return meta

    def search_topk(self, query_vector: np.ndarray, k: int) -> list[ChunkHit]:
        query = query_vector.astype(np.float32).reshape(-1)
        dim = self.matrix.shape[1]
        if query.shape != (dim,):
            raise ValueError(f"query shape {query.shape} != ({dim},)")
        if not self.rows:
            return []
        q_norm = float(np.linalg.norm(query))
        if q_norm == 0.0:
            first: dict[str, int] = {}
            for index, row in enumerate(self.rows):
                _ = first.setdefault(row.path, index)
            return [self._hit(index, 0.0) for index in list(first.values())[:k]]
        row_norms = np.linalg.norm(self.matrix, axis=1)
        scores = [array_number(c) for c in array_items(self.matrix @ query / (row_norms * q_norm))]
        best: dict[str, int] = {}
        for index, row in enumerate(self.rows):
            if row.path not in best or scores[index] > scores[best[row.path]]:
                best[row.path] = index
        ranked = sorted(best.values(), key=lambda index: scores[index], reverse=True)[:k]
        return [self._hit(index, scores[index]) for index in ranked]

    def _hit(self, index: int, score: float) -> ChunkHit:
        row = self.rows[index]
        return ChunkHit(row.path, row.kind, row.chunk_idx, score)
