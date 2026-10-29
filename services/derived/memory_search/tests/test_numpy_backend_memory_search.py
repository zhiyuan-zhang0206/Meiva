"""NumPyBackend client tests — against a live memory_search service.

A session-scoped uvicorn server on a free loopback port serves the real
app (store + npz in a tmp dir), so the backend's HTTP client is exercised
over a real socket — the same shape the indexer daemon and gateway dial.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi import FastAPI

from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.host.net.tests.local_http import serve_asgi
from services.derived.memory_indexer.backends import probe
from services.derived.memory_indexer.backends.numpy import NumPyBackend
from services.derived.memory_search.tests.search_service import free_port, serve_memory_search

_DIM = 8
_FP = "test:gemini:dim=8"


def _vec(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(_DIM).astype(np.float32)


@pytest.fixture(scope="session")
def memory_search_uri(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A real uvicorn server for the whole session, on a free port."""
    data_file = tmp_path_factory.mktemp("memory-search") / "vectors.npz"
    with serve_memory_search(data_file, dim=_DIM, fingerprint=_FP) as uri:
        yield uri


@pytest.fixture
def backend(memory_search_uri: str) -> Iterator[NumPyBackend]:
    b = NumPyBackend(fingerprint=_FP, uri=memory_search_uri)
    b.connect()
    try:
        yield b
    finally:
        b.close()


def test_write_read_roundtrip(backend: NumPyBackend) -> None:
    ones = np.ones(_DIM, dtype=np.float32)
    backend.upsert("/a.md", 1.0, "ha", ones, kind="body", chunk_idx=0)
    backend.upsert("/b.md", 2.0, "hb", -ones, kind="body", chunk_idx=0)
    assert backend.all_meta() == {"/a.md": (1.0, "ha", _FP), "/b.md": (2.0, "hb", _FP)}
    assert [hit.path for hit in backend.search_topk(ones, k=5)] == ["/a.md", "/b.md"]
    backend.delete("/a.md")
    assert backend.all_meta() == {"/b.md": (2.0, "hb", _FP)}


def test_upsert_many_roundtrip(backend: NumPyBackend) -> None:
    rows = [
        ("/batch-a.md", 1.0, "ha", _vec(0), "body", 0),
        ("/batch-b.md", 2.0, "hb", _vec(1), "body", 0),
    ]
    backend.upsert_many(rows)
    meta = backend.all_meta()
    assert meta["/batch-a.md"] == (1.0, "ha", _FP)
    assert meta["/batch-b.md"] == (2.0, "hb", _FP)
    results = backend.search_topk(_vec(0), k=1000)
    assert {"/batch-a.md", "/batch-b.md"}.issubset(hit.path for hit in results)
    backend.delete("/batch-a.md")
    backend.delete("/batch-b.md")


def test_upsert_many_uses_one_request_with_batch_timeout() -> None:
    requests: list[httpx.Request] = []

    def _respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok"})

    backend = NumPyBackend(fingerprint=_FP, uri="http://memory-search")
    backend._client = httpx.Client(
        base_url="http://memory-search", transport=httpx.MockTransport(_respond)
    )
    try:
        backend.upsert_many(
            [
                ("/a.md", 1.0, "ha", _vec(0), "body", 0),
                ("/b.md", 2.0, "hb", _vec(1), "body", 0),
            ]
        )
    finally:
        backend.close()

    assert len(requests) == 1
    assert requests[0].url.path == "/v1/upsert_batch"
    assert requests[0].extensions["timeout"]["read"] == 300.0


def test_readonly_upsert_many_connects_but_refuses_write(memory_search_uri: str) -> None:
    backend = NumPyBackend(fingerprint=_FP, readonly=True, uri=memory_search_uri)
    backend.connect()
    try:
        with pytest.raises(RuntimeError, match="read-only"):
            backend.upsert_many([("/a.md", 1.0, "ha", _vec(0), "body", 0)])
    finally:
        backend.close()


def test_search_topk_async_matches_sync(backend: NumPyBackend) -> None:
    ones = np.ones(_DIM, dtype=np.float32)
    backend.upsert("/a.md", 1.0, "ha", ones, kind="body", chunk_idx=0)
    backend.upsert("/b.md", 2.0, "hb", -ones, kind="body", chunk_idx=0)
    sync = backend.search_topk(ones, k=5)
    async_result = asyncio.run(backend.search_topk_async(ones, 5, timeout=5.0))
    assert async_result == sync
    assert [(hit.path, hit.kind, hit.chunk_idx) for hit in sync] == [
        ("/a.md", "body", 0),
        ("/b.md", "body", 0),
    ]
    assert sync[0].score == pytest.approx(1.0)


def test_each_backend_dials_the_uri_it_was_given(memory_search_uri: str, tmp_path: Path) -> None:
    ones = np.ones(_DIM, dtype=np.float32)
    with serve_memory_search(tmp_path / "other.npz", dim=_DIM, fingerprint=_FP) as other_uri:
        shared, other = (
            NumPyBackend(fingerprint=_FP, uri=memory_search_uri),
            NumPyBackend(fingerprint=_FP, uri=other_uri),
        )
        shared.connect()
        other.connect()
        try:
            other.upsert("/only-other.md", 1.0, "h", ones, kind="body", chunk_idx=0)
            assert "/only-other.md" in other.all_meta()
            assert "/only-other.md" not in shared.all_meta()
            other_hits = asyncio.run(other.search_topk_async(ones, 5, timeout=5.0))
            shared_hits = asyncio.run(shared.search_topk_async(ones, 5, timeout=5.0))
            assert [hit.path for hit in other_hits] == ["/only-other.md"]
            assert "/only-other.md" not in [hit.path for hit in shared_hits]
        finally:
            shared.close()
            other.close()


def test_connect_fails_fast_when_service_down() -> None:
    backend = NumPyBackend(fingerprint=_FP, uri=f"http://127.0.0.1:{free_port()}")
    with pytest.raises(httpx.ConnectError):
        backend.connect()
    backend.close()  # idempotent no-op


def test_requires_connect_before_use() -> None:
    backend = NumPyBackend(fingerprint=_FP, uri="http://memory-search")
    with pytest.raises(RuntimeError, match="not connected"):
        backend.all_meta()


# ── preflight probes (CTO ruling 2026-08-30 direction ②) ───────────────────


def test_probe_numpy_healthy(memory_search_uri: str, *, database_gate: ProcessDbGate) -> None:
    assert (
        probe.probe_backend(
            "numpy",
            Database.from_settings(gate=database_gate),
            uri=memory_search_uri,
            fingerprint=_FP,
        ).message
        is None
    )


def test_new_client_refuses_legacy_protocol_before_any_write() -> None:
    legacy = FastAPI()
    writes: list[str] = []

    @legacy.get("/meta")
    async def meta() -> dict[str, object]:
        return {}

    @legacy.post("/upsert")
    async def upsert() -> dict[str, str]:
        writes.append("wrong-generation-write")
        return {"status": "ok"}

    with serve_asgi(legacy) as uri:
        backend = NumPyBackend(uri=uri, fingerprint=_FP)
        with pytest.raises(httpx.HTTPStatusError) as caught:
            backend.connect()
        assert caught.value.response.status_code == 404
        with pytest.raises(RuntimeError, match="not connected"):
            backend.upsert("/wrong.md", 1.0, "h", _vec(0))
        assert writes == []
        backend.close()
