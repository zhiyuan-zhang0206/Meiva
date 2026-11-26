# meiva:link services/docs/gateway_side/memory_indexer/memory_indexer.md

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeGuard
from zipfile import BadZipFile

import numpy as np
from numpy.lib.npyio import NpzFile

from base.host.boundary.untyped import object_of
from services.derived.memory_search.snapshot import Row, Snapshot, array_items, array_number

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


def _is_npz(value: object) -> TypeGuard[NpzFile[np.generic]]:
    return isinstance(value, NpzFile)


def _read_snapshot(data: NpzFile[np.generic], dim: int, fingerprint: str) -> Snapshot:
    matrix = data[_NPZ_VECTORS].astype(np.float32)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1).copy()
    keys = zip(
        array_items(data[_NPZ_PKS]),
        array_items(data[_NPZ_PATHS]),
        array_items(data[_NPZ_KINDS]),
        strict=True,
    )
    values = zip(
        array_items(data[_NPZ_CHUNK_IDX]),
        array_items(data[_NPZ_MTIMES]),
        array_items(data[_NPZ_HASHES]),
        strict=True,
    )
    rows = tuple(
        Row(str(pk), str(path), str(kind), int(array_number(idx)), array_number(mtime), str(hash_))
        for (pk, path, kind), (idx, mtime, hash_) in zip(keys, values, strict=True)
    )
    embedders = array_items(data[_NPZ_EMBEDDERS])
    if (
        matrix.shape != (len(rows), dim)
        or len(embedders) != len(rows)
        or any(space != fingerprint for space in embedders)
    ):
        raise ValueError("cache has an incompatible vector space")
    return Snapshot.build(rows, matrix, fingerprint)


def _load_archive(data_file: Path, dim: int, fingerprint: str) -> Snapshot:
    opened = object_of(np.load, data_file)
    if not _is_npz(opened):
        raise ValueError("not an npz archive")
    with opened as data:
        missing = _NPZ_KEYS - set(data.files)
        if missing:
            raise ValueError(f"missing keys {sorted(missing)}")
        return _read_snapshot(data, dim, fingerprint)


def load_snapshot(data_file: Path, dim: int, fingerprint: str) -> Snapshot | None:
    if not data_file.exists():
        return None
    try:
        return _load_archive(data_file, dim, fingerprint)
    except (OSError, ValueError, EOFError, KeyError, BadZipFile) as exc:
        _log.warning(
            "[memory_search] %s does not parse or fully convert (%s: %s) — "
            "starting empty; cold-start will rebuild the index",
            data_file,
            type(exc).__name__,
            exc,
        )
        return None


def write_npz(path: Path, snapshot: Snapshot) -> None:
    rows = snapshot.rows
    np.savez(
        path,
        pks=np.array([row.pk for row in rows], dtype=str),
        paths=np.array([row.path for row in rows], dtype=str),
        kinds=np.array([row.kind for row in rows], dtype=str),
        chunk_idx=np.array([row.chunk_idx for row in rows], dtype=np.int64),
        mtimes=np.array([row.mtime for row in rows], dtype=np.float64),
        content_hashes=np.array([row.content_hash for row in rows], dtype=str),
        embedders=np.array([snapshot.fingerprint] * len(rows), dtype=str),
        vectors=snapshot.matrix,
    )


def save_snapshot(
    data_file: Path,
    snapshot: Snapshot,
    replace_file: Callable[[Path, Path], object],
    write_archive: Callable[[Path, Snapshot], None],
) -> float:
    start = time.perf_counter()
    data_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = data_file.with_suffix(".tmp.npz")
    try:
        write_archive(tmp, snapshot)
        _ = replace_file(tmp, data_file)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            _log.warning("[memory_search] failed to clean temporary archive %s", tmp, exc_info=True)
        raise
    return time.perf_counter() - start
