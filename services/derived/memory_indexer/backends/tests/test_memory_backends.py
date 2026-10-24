from __future__ import annotations

import numpy as np
import pytest

from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from services.derived.memory_indexer.backends import factory
from services.derived.memory_indexer.backends.base import KIND_DESC, pk_of
from services.derived.memory_indexer.backends.numpy import NumPyBackend
from services.derived.memory_indexer.backends.pgvector import PGVectorBackend

_DIM = 8
_FP = "test:gemini:dim=8"
# meiva:note
# never dialed: nothing here connects
_URI = "http://127.0.0.1:1"


def _vec(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(_DIM).astype(np.float32)


def test_factory_and_probe_registries_stay_in_sync() -> None:
    from services.derived.memory_indexer.backends import probe

    assert set(factory.BACKENDS) == set(probe.PROBES)


def test_factory_default_is_numpy(*, database_gate: ProcessDbGate) -> None:
    services = ConfigBoot().view.services
    assert services.memory_search_backend == "numpy"
    assert isinstance(
        factory.get_backend(
            services.memory_search_backend,
            database=Database.from_settings(gate=database_gate),
            dim=_DIM,
            fingerprint=_FP,
            uri=services.memory_search_uri,
        ),
        NumPyBackend,
    )


@pytest.mark.parametrize(
    ("name", "backend_class"), [("numpy", NumPyBackend), ("pgvector", PGVectorBackend)]
)
def test_factory_dispatches_on_the_name_it_is_given(
    name: str, backend_class: type, *, database_gate: ProcessDbGate
) -> None:
    assert isinstance(
        factory.get_backend(
            name,
            database=Database.from_settings(gate=database_gate),
            dim=_DIM,
            fingerprint=_FP,
            uri=_URI,
        ),
        backend_class,
    )


def test_factory_unknown_backend_fails_fast(*, database_gate: ProcessDbGate) -> None:
    with pytest.raises(ValueError, match="unknown memory search backend"):
        factory.get_backend(
            "qdrant",
            database=Database.from_settings(gate=database_gate),
            dim=_DIM,
            fingerprint=_FP,
            uri=_URI,
        )


def test_pk_of_roundtrips_path_kind_chunk() -> None:
    assert pk_of("/a/b.md", KIND_DESC, 0) == "/a/b.md\x1fdesc\x1f0"
    assert pk_of("/a/b.md", "body", 3) == "/a/b.md\x1fbody\x1f3"


def test_empty_upsert_many_is_noop_before_connect(*, database_gate: ProcessDbGate) -> None:
    for name in (PGVectorBackend.name, NumPyBackend.name):
        backend = factory.get_backend(
            name,
            database=Database.from_settings(gate=database_gate),
            dim=_DIM,
            fingerprint=_FP,
            uri=_URI,
        )
        backend.upsert_many([])


def test_readonly_backends_refuse_mutations(*, database_gate: ProcessDbGate) -> None:
    for name in (PGVectorBackend.name, NumPyBackend.name):
        backend = factory.get_backend(
            name,
            database=Database.from_settings(gate=database_gate),
            dim=_DIM,
            fingerprint=_FP,
            uri=_URI,
            readonly=True,
        )
        with pytest.raises(RuntimeError, match="read-only"):
            backend.upsert("/a.md", 1.0, "hash", _vec(0), kind="body", chunk_idx=0)
        with pytest.raises(RuntimeError, match="read-only"):
            backend.upsert_many([("/a.md", 1.0, "hash", _vec(0), "body", 0)])
        with pytest.raises(RuntimeError, match="read-only"):
            backend.delete("/a.md")
