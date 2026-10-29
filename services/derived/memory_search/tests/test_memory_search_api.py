from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from services.derived.memory_search.app import build_app
from services.derived.memory_search.store import MemoryStore

_DIM = 8
_FP = "test:gemini:dim=8"
_MAX_BATCH_ROWS = 6


def _vec(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(_DIM).astype(np.float32)


def _store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "vectors.npz", dim=_DIM, fingerprint=_FP)


def _client(tmp_path: Path) -> TestClient:
    return TestClient(
        build_app(_store(tmp_path), _MAX_BATCH_ROWS, embedding_dim=_DIM),
        headers={"x-meiva-embedding-space": _FP},
    )


def _upsert_body(path: str, seed: int, *, mtime: float = 1.0) -> dict[str, object]:
    return {
        "path": path,
        "mtime": mtime,
        "content_hash": f"hash-{seed}",
        "kind": "body",
        "chunk_idx": 0,
        "vector": _vec(seed).tolist(),
    }


def test_healthz(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert client.get("/healthz").json() == {"status": "ok"}


def test_upsert_delete_meta_roundtrip(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert (
            client.post(
                "/v1/upsert",
                json={
                    "path": "/a.md",
                    "mtime": 1.0,
                    "content_hash": "ha",
                    "kind": "body",
                    "chunk_idx": 0,
                    "vector": _vec(0).tolist(),
                },
            ).status_code
            == 200
        )
        assert client.get("/v1/meta").json() == {"/a.md": [1.0, "ha", _FP]}
        assert client.post("/v1/delete", json={"path": "/a.md"}).status_code == 200
        assert client.get("/v1/meta").json() == {}


def test_delete_stale_batch_removes_tail_and_persists(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        for idx in range(3):
            client.post(
                "/v1/upsert",
                json=_upsert_body("/a.md", idx, mtime=1.0) | {"chunk_idx": idx},
            )
        assert client.get("/stats").json()["rows"] == 3
        resp = client.post(
            "/v1/delete_stale_batch",
            json={"entries": [{"path": "/a.md", "kind_limits": {"body": 1}}]},
        )
        assert resp.status_code == 200
        assert client.get("/stats").json()["rows"] == 1
        assert client.get("/v1/meta").json() == {"/a.md": [1.0, "hash-0", _FP]}

        from services.derived.memory_search.store import MemoryStore as _Store

        reloaded = _Store(tmp_path / "vectors.npz", dim=_DIM, fingerprint=_FP)
        reloaded.load()
        assert len(reloaded) == 1


def test_search_returns_ordered_hits(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        ones = np.ones(_DIM, dtype=np.float32)
        for path, vector in [("/a.md", ones), ("/b.md", -ones)]:
            assert (
                client.post(
                    "/v1/upsert",
                    json={
                        "path": path,
                        "mtime": 1.0,
                        "content_hash": "h",
                        "kind": "body",
                        "chunk_idx": 0,
                        "vector": vector.tolist(),
                    },
                ).status_code
                == 200
            )
        body = client.post("/v1/search", json={"vector": ones.tolist(), "k": 5}).json()
        assert [(h["path"], h["kind"], h["chunk_idx"]) for h in body["hits"]] == [
            ("/a.md", "body", 0),
            ("/b.md", "body", 0),
        ]
        assert body["hits"][0]["score"] == pytest.approx(1.0)
        assert body["hits"][1]["score"] == pytest.approx(-1.0)
        assert set(body) == {"hits"}


def test_upsert_persists_before_ack(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        ones = np.ones(_DIM, dtype=np.float32)
        assert (
            client.post(
                "/v1/upsert",
                json={
                    "path": "/a.md",
                    "mtime": 1.0,
                    "content_hash": "ha",
                    "kind": "body",
                    "chunk_idx": 0,
                    "vector": ones.tolist(),
                },
            ).status_code
            == 200
        )
    fresh = _store(tmp_path)
    fresh.load()
    assert fresh.all_meta() == {"/a.md": (1.0, "ha", _FP)}
    assert [hit.path for hit in fresh.search_topk(ones, k=5)] == ["/a.md"]


def test_upsert_batch_persists_all_rows_before_ack(tmp_path: Path) -> None:
    rows = [_upsert_body("/batch-a.md", 0), _upsert_body("/batch-b.md", 1)]
    with _client(tmp_path) as client:
        assert client.post("/v1/upsert_batch", json={"rows": rows}).json() == {"status": "ok"}

    fresh = _store(tmp_path)
    fresh.load()
    assert fresh.all_meta() == {
        "/batch-a.md": (1.0, "hash-0", _FP),
        "/batch-b.md": (1.0, "hash-1", _FP),
    }
    assert len(fresh) == 2


def test_upsert_batch_matches_sequential_endpoints(tmp_path: Path) -> None:
    rows = [_upsert_body("/a.md", 0), _upsert_body("/b.md", 1), _upsert_body("/c.md", 2)]
    with _client(tmp_path / "sequential") as sequential, _client(tmp_path / "batched") as batched:
        for row in rows:
            assert sequential.post("/v1/upsert", json=row).status_code == 200
        assert batched.post("/v1/upsert_batch", json={"rows": rows}).status_code == 200

        assert batched.get("/v1/meta").json() == sequential.get("/v1/meta").json()
        query = {"vector": _vec(0).tolist(), "k": 10}
        assert (
            batched.post("/v1/search", json=query).json()
            == sequential.post("/v1/search", json=query).json()
        )
        stats = [client.get("/stats").json() for client in (sequential, batched)]
        assert [entry["rows"] for entry in stats] == [3, 3]
        assert all(entry["last_save_seconds"] >= 0.0 for entry in stats)


def test_upsert_batch_saves_once_while_single_upserts_save_each_row(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    rows = [_upsert_body(f"/{idx}.md", idx) for idx in range(5)]
    with TestClient(
        build_app(store, _MAX_BATCH_ROWS, embedding_dim=_DIM),
        headers={"x-meiva-embedding-space": _FP},
    ) as client:
        assert client.post("/v1/upsert_batch", json={"rows": rows}).status_code == 200
        assert store.saves == 1
        for row in rows:
            assert client.post("/v1/upsert", json=row).status_code == 200
        assert store.saves == 1 + len(rows)


def test_batch_rows_cap_is_the_one_the_app_was_built_with(tmp_path: Path) -> None:
    rows = [_upsert_body(f"/{idx}.md", idx) for idx in range(3)]
    empty_limits: dict[str, int] = {}
    entries = [{"path": row["path"], "kind_limits": empty_limits} for row in rows]
    with TestClient(
        build_app(_store(tmp_path), 2, embedding_dim=_DIM), headers={"x-meiva-embedding-space": _FP}
    ) as small:
        assert small.post("/v1/upsert_batch", json={"rows": rows}).status_code == 422
        assert small.post("/v1/delete_stale_batch", json={"entries": entries}).status_code == 422
    with TestClient(
        build_app(_store(tmp_path), 3, embedding_dim=_DIM), headers={"x-meiva-embedding-space": _FP}
    ) as exact:
        assert exact.post("/v1/upsert_batch", json={"rows": rows}).status_code == 200
        assert exact.post("/v1/delete_stale_batch", json={"entries": entries}).status_code == 200


def test_upsert_batch_rejects_empty_oversized_and_wrong_dim(tmp_path: Path) -> None:
    row = _upsert_body("/a.md", 0)
    bad_dim = {**row, "vector": [0.0] * (_DIM - 1)}
    with _client(tmp_path) as client:
        assert client.post("/v1/upsert_batch", json={"rows": []}).status_code == 422
        assert (
            client.post(
                "/v1/upsert_batch", json={"rows": [row] * (_MAX_BATCH_ROWS + 1)}
            ).status_code
            == 422
        )
        response = client.post("/v1/upsert_batch", json={"rows": [row, bad_dim]})
        assert response.status_code == 422
        assert response.json()["detail"] == f"embedding shape ({_DIM - 1},) != ({_DIM},)"


def test_stats_empty_fresh_store(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert client.get("/stats").json() == {"rows": 0, "last_save_seconds": None}


def test_stats_after_upsert(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        for path, seed in [("/a.md", 0), ("/b.md", 1)]:
            assert (
                client.post(
                    "/v1/upsert",
                    json={
                        "path": path,
                        "mtime": 1.0,
                        "content_hash": "h",
                        "kind": "body",
                        "chunk_idx": 0,
                        "vector": _vec(seed).tolist(),
                    },
                ).status_code
                == 200
            )
        body = client.get("/stats").json()
        assert body["rows"] == 2
        assert isinstance(body["last_save_seconds"], float)
        assert body["last_save_seconds"] >= 0.0
        assert client.post("/v1/delete", json={"path": "/a.md"}).status_code == 200
        assert client.get("/stats").json()["rows"] == 1


def test_emit_memory_search_stats(emitted_events: EmittedEvents) -> None:
    import services.derived.memory_search.app as app_module

    app_module.emit_memory_search_stats(5, None)
    app_module.emit_memory_search_stats(5, 0.25)
    assert [(event.category, event.event_name, event.attributes) for event in emitted_events] == [
        ("telemetry", "memory_search_stats", {"rows": 5}),
        ("telemetry", "memory_search_stats", {"rows": 5, "last_save_seconds": 0.25}),
    ]


def test_upsert_rejects_wrong_dim_vector(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.post(
            "/v1/upsert",
            json={
                "path": "/a.md",
                "mtime": 1.0,
                "content_hash": "h",
                "kind": "body",
                "chunk_idx": 0,
                "vector": [0.0] * 7,
            },
        )
        assert resp.status_code == 422


def test_upsert_rejects_oversized_vector(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.post(
            "/v1/upsert",
            json={
                "path": "/a.md",
                "mtime": 1.0,
                "content_hash": "h",
                "kind": "body",
                "chunk_idx": 0,
                "vector": [0.0] * (_DIM + 1),
            },
        )
        assert resp.status_code == 422


def test_search_rejects_oversized_vector_and_bad_k(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert (
            client.post("/v1/search", json={"vector": [0.0] * (_DIM + 1), "k": 5}).status_code
            == 422
        )
        assert client.post("/v1/search", json={"vector": [0.0] * _DIM, "k": 0}).status_code == 422
        assert (
            client.post("/v1/search", json={"vector": [0.0] * _DIM, "k": 1001}).status_code == 422
        )


@pytest.mark.parametrize("fingerprint", [None, "different-space"])
def test_space_mismatch_rejects_same_width_search_and_write(
    tmp_path: Path, fingerprint: str | None
) -> None:
    headers = {} if fingerprint is None else {"x-meiva-embedding-space": fingerprint}
    with TestClient(build_app(_store(tmp_path), _MAX_BATCH_ROWS, embedding_dim=_DIM)) as client:
        assert (
            client.post(
                "/v1/search", headers=headers, json={"vector": _vec(1).tolist(), "k": 1}
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/v1/upsert", headers=headers, json=_upsert_body("/wrong.md", 1)
            ).status_code
            == 409
        )
        assert client.get("/stats").json()["rows"] == 0


@pytest.mark.parametrize(
    "path", ["/search", "/upsert", "/upsert_batch", "/delete", "/delete_stale_batch"]
)
def test_legacy_clients_have_no_mutating_or_query_route(tmp_path: Path, path: str) -> None:
    with _client(tmp_path) as client:
        assert client.post(path, json={}).status_code == 404
        assert client.get("/meta").status_code == 404
        assert client.get("/stats").json()["rows"] == 0
