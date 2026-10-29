"""Memory indexer unit tests — daemon reconcile + chunk commit logic.

The embedding provider is mocked (a `FakeProvider`); storage is the real
`MemoryStore` behind an in-process adapter (`store_backend.StoreBackend`), so
the daemon's reconcile, tail-cleanup and provider-switch behavior runs against
the production numpy storage core without the HTTP hop. The store's own
semantics are pinned in `services/derived/memory_search/tests/test_memory_search_store.py`.

The provider contract (Gemini adapter wire behavior) is pinned separately
in `services/derived/memory_indexer/embeddings/tests/test_embeddings.py`; here the focus is the daemon's
reconcile logic, including the provider-fingerprint gate (a provider
switch re-embeds every row even at the same content hash — same dim is
not the same semantic space).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

import numpy as np
import pytest

from base.daemon.health import Liveness
from base.packages.plugin_providers import build_model_catalog
from base.paths import meiva_home
from services.derived.memory_indexer import config, daemon
from services.derived.memory_indexer.backends.base import (
    ChunkHit,
    MemorySearchBackend,
    content_hash,
)
from services.derived.memory_indexer.config import MemoryIndexerConfig
from services.derived.memory_indexer.embeddings import factory
from services.derived.memory_indexer.embeddings.base import EmbeddingAPIError
from services.derived.memory_indexer.tests.fake_provider import (
    DIM,
    FINGERPRINT,
    FakeProvider,
)
from services.derived.memory_search.tests.store_backend import StoreBackend

PROVIDER = "gemini"
EMBED_TIMEOUT_S = 60.0


def liveness_timeout() -> float:
    return config.liveness_timeout_seconds(PROVIDER, timeout_seconds=EMBED_TIMEOUT_S)


@pytest.fixture
def store_backend(tmp_path: Path) -> StoreBackend:
    """A fresh in-process backend over a real `MemoryStore` (npz under tmp_path)."""
    return StoreBackend(tmp_path / "index" / "vectors.npz", dim=DIM, fingerprint=FINGERPRINT)


def _vec(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(DIM).astype(np.float32)


@pytest.fixture
def watched_root(tmp_path: Path) -> Path:
    """The root the daemon indexes: this test's sandbox, resolved as the daemon resolves its own.

    `process_paths` prunes any path outside the root it is given (stale
    authoring-checkout leftovers), so tests that embed files under
    `tmp_path` must hand it `tmp_path` — otherwise every file they write
    would count as foreign and be deleted.
    """
    return tmp_path.resolve()


def test_content_hash_deterministic() -> None:
    assert content_hash("hello") == content_hash("hello")
    assert content_hash("hello") != content_hash("world")


# ── daemon helpers ──────────────────────────────────────────────────────


def test_scan_disk_finds_md_only(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.md").write_text("c")

    result = daemon.scan_disk(tmp_path, config=MemoryIndexerConfig())
    assert result == {
        (tmp_path / "a.md").resolve(): content_hash("a"),
        (sub / "c.md").resolve(): content_hash("c"),
    }


def test_scan_disk_skips_navigation_files(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text("- [a](a.md) — a")
    (tmp_path / "a.md").write_text("a")
    (tmp_path / "index.md").write_text("- [a](a.md) — a")
    (tmp_path / "index-of-rulings.md").write_text("a real note")

    result = daemon.scan_disk(tmp_path, config=MemoryIndexerConfig())
    assert set(result) == {
        (tmp_path / "a.md").resolve(),
        (tmp_path / "index-of-rulings.md").resolve(),
    }


def test_scan_disk_skips_symlinks(tmp_path: Path) -> None:
    real = tmp_path / "real.md"
    real.write_text("a")
    link = tmp_path / "link.md"
    link.symlink_to(real)

    result = daemon.scan_disk(tmp_path, config=MemoryIndexerConfig())
    assert link.resolve() not in result or set(result.keys()) == {real.resolve()}


def test_scan_disk_missing_root_returns_empty(tmp_path: Path) -> None:
    result = daemon.scan_disk(tmp_path / "nonexistent", config=MemoryIndexerConfig())
    assert result == {}


def test_process_paths_embeds_new_files(
    tmp_path: Path,
    store_backend: StoreBackend,
    watched_root: Path,
) -> None:
    f1 = tmp_path / "a.md"
    f1.write_text("content A")
    f2 = tmp_path / "b.md"
    f2.write_text("content B")

    provider = FakeProvider()
    daemon.process_paths(
        store_backend,
        {f1.resolve(), f2.resolve()},
        provider,
        Liveness(liveness_timeout()),
        root=watched_root,
        config=MemoryIndexerConfig(),
    )
    assert provider.embed_batch_count == 1
    meta = store_backend.all_meta()
    assert str(f1.resolve()) in meta
    assert str(f2.resolve()) in meta


def test_process_paths_skips_unchanged_hash(
    tmp_path: Path,
    store_backend: StoreBackend,
    watched_root: Path,
) -> None:
    f = tmp_path / "a.md"
    f.write_text("content")

    provider = FakeProvider()
    daemon.process_paths(
        store_backend,
        {f.resolve()},
        provider,
        Liveness(liveness_timeout()),
        root=watched_root,
        config=MemoryIndexerConfig(),
    )
    assert provider.embed_batch_count == 1

    daemon.process_paths(
        store_backend,
        {f.resolve()},
        provider,
        Liveness(liveness_timeout()),
        root=watched_root,
        config=MemoryIndexerConfig(),
    )
    assert provider.embed_batch_count == 1  # hash unchanged, no re-embed


def test_process_paths_reembeds_on_provider_fingerprint_change(
    tmp_path: Path,
    store_backend: StoreBackend,
    watched_root: Path,
) -> None:
    """CTO ① (2026-08-30): a provider switch must trigger a full rebuild —
    old vectors live in a different semantic space even at the same dim, so
    they cannot be mixed with new ones (same content hash is irrelevant)."""
    f = tmp_path / "a.md"
    f.write_text("content")

    first = FakeProvider()
    daemon.process_paths(
        store_backend,
        {f.resolve()},
        first,
        Liveness(liveness_timeout()),
        root=watched_root,
        config=MemoryIndexerConfig(),
    )
    assert first.embed_batch_count == 1

    # Simulate the switch: same content, same mtime, new provider fingerprint —
    # a fresh daemon run would build the backend for the new provider too.
    switched = FakeProvider(fingerprint="another-provider:dim=8")
    switched_backend = store_backend.reopen("another-provider:dim=8")
    daemon.process_paths(
        switched_backend,
        {f.resolve()},
        switched,
        Liveness(liveness_timeout()),
        root=watched_root,
        config=MemoryIndexerConfig(),
    )
    assert switched.embed_batch_count == 1  # re-embedded despite unchanged hash
    meta = switched_backend.all_meta()
    assert meta[str(f.resolve())][2] == "another-provider:dim=8"


def test_process_paths_deletes_missing_files(
    tmp_path: Path, store_backend: StoreBackend, *, watched_root: Path
) -> None:
    """Path enters dirty set but file not on disk — index row is deleted."""
    ghost = str(
        tmp_path / "ghost_nonexistent.md"
    )  # under tmp_path, definitely does not exist (never written)
    store_backend.upsert(ghost, 1.0, "h", _vec(0), kind="body", chunk_idx=0)
    daemon.process_paths(
        store_backend,
        {Path(ghost)},
        FakeProvider(),
        Liveness(liveness_timeout()),
        root=watched_root,
        config=MemoryIndexerConfig(),
    )
    assert store_backend.all_meta() == {}


def test_process_paths_deletes_foreign_paths_even_when_file_exists(
    tmp_path: Path, store_backend: StoreBackend
) -> None:
    """Rows outside the watched root are pruned even when the file still
    exists on disk — the stale authoring-checkout leftovers that surface
    as duplicate search hits (e.g. user-profile.md ×2)."""
    watched = tmp_path / "watched"
    watched.mkdir()
    foreign_dir = tmp_path / "foreign"
    foreign_dir.mkdir()
    foreign = foreign_dir / "note.md"  # exists on disk, outside watched root
    foreign.write_text("content")

    store_backend.upsert(str(foreign), 1.0, "h", _vec(0), kind="body", chunk_idx=0)
    daemon.process_paths(
        store_backend,
        {foreign},
        FakeProvider(),
        Liveness(liveness_timeout()),
        root=watched.resolve(),
        config=MemoryIndexerConfig(),
    )
    assert store_backend.all_meta() == {}


def test_reconcile_prunes_foreign_rows(tmp_path: Path, store_backend: StoreBackend) -> None:
    root = tmp_path / "watched"
    root.mkdir()
    watched = root / "a.md"
    watched.write_text("watched content")
    foreign = tmp_path / "foreign.md"
    foreign.write_text("foreign content")

    backend = store_backend
    backend.upsert(str(watched.resolve()), 1.0, "h", _vec(0), kind="body", chunk_idx=0)
    backend.upsert(str(foreign.resolve()), 1.0, "h", _vec(0), kind="body", chunk_idx=0)

    daemon.reconcile(
        backend,
        FakeProvider(),
        Liveness(liveness_timeout()),
        config=MemoryIndexerConfig(),
        status=daemon.PatrolStatus(retry_limit=3),
        root=root,
    )
    meta = store_backend.all_meta()
    assert str(foreign.resolve()) not in meta
    assert str(watched.resolve()) in meta


def test_reconcile_reembeds_on_provider_switch(tmp_path: Path, store_backend: StoreBackend) -> None:
    root = tmp_path / "watched"
    root.mkdir()
    watched = root / "a.md"
    watched.write_text("watched content")

    backend = store_backend
    watched_mtime = watched.stat().st_mtime
    backend.upsert(str(watched.resolve()), watched_mtime, "h", _vec(0), kind="body", chunk_idx=0)
    assert backend.all_meta() == {str(watched.resolve()): (watched_mtime, "h", FINGERPRINT)}

    switched = FakeProvider(fingerprint="other:provider")
    switched_backend = store_backend.reopen("other:provider")
    daemon.reconcile(
        switched_backend,
        switched,
        Liveness(liveness_timeout()),
        config=MemoryIndexerConfig(),
        status=daemon.PatrolStatus(retry_limit=3),
        root=root,
    )
    meta = switched_backend.all_meta()
    assert str(watched.resolve()) in meta
    assert meta[str(watched.resolve())][2] == "other:provider"


class _RecordingBackend(MemorySearchBackend):
    """In-memory writes isolate beat placement from RPC and filesystem latency."""

    name = "recording"

    def __init__(self, spend: Callable[[str], None] = lambda _op: None) -> None:
        self.rows: dict[tuple[str, str, int], tuple[float, str, str]] = {}
        self.calls: list[str] = []
        self.spend = spend

    def connect(self) -> None:
        raise AssertionError("processing must use the already connected backend")

    def close(self) -> None:
        raise AssertionError("processing must leave backend lifecycle to its caller")

    def upsert(
        self,
        path: str,
        mtime: float,
        content_hash: str,
        embedding: np.ndarray,
        *,
        kind: str,
        chunk_idx: int,
    ) -> None:
        self.upsert_many([(path, mtime, content_hash, embedding, kind, chunk_idx)])

    def search_topk(self, query_vector: np.ndarray, k: int) -> list[ChunkHit]:
        raise AssertionError("indexing must not search")

    async def search_topk_async(
        self, query_vector: np.ndarray, k: int, *, timeout: float
    ) -> list[ChunkHit]:
        raise AssertionError("indexing must not search")

    def all_meta(self) -> dict[str, tuple[float, str, str]]:
        return {path: meta for (path, _, _), meta in self.rows.items()}

    def delete(self, path: str) -> None:
        self.calls.append("delete")
        self.spend("delete")
        self.rows = {key: meta for key, meta in self.rows.items() if key[0] != path}

    def delete_stale_rows(self, entries: Sequence[tuple[str, dict[str, int]]]) -> None:
        self.calls.append("delete_stale_rows")
        self.spend("delete_stale_rows")
        for path, limits in entries:
            self.rows = {
                key: meta
                for key, meta in self.rows.items()
                if key[0] != path or (key[1] in limits and key[2] < limits[key[1]])
            }

    def upsert_many(self, rows: Sequence[tuple[str, float, str, np.ndarray, str, int]]) -> None:
        self.calls.append("upsert_many")
        self.spend("upsert_many")
        for path, mtime, hash_, _, kind, idx in rows:
            self.rows[path, kind, idx] = (mtime, hash_, FINGERPRINT)


class _ManualLiveness:
    def __init__(self, timeout_s: float, monotonic: Callable[[], float]) -> None:
        self._timeout_s = timeout_s
        self._monotonic = monotonic
        self._last = monotonic()

    def beat(self) -> None:
        self._last = self._monotonic()

    def stale_for(self) -> float:
        return self._monotonic() - self._last

    def is_alive(self) -> bool:
        return self.stale_for() <= self._timeout_s


def _manual_liveness(timeout_s: float, monotonic: Callable[[], float]) -> Liveness:
    return cast(Liveness, _ManualLiveness(timeout_s, monotonic))


def test_process_paths_beats_per_embed_batch(tmp_path: Path, *, watched_root: Path) -> None:
    """Every batch gets its own beat, even when their total exceeds the ceiling."""
    now = 0.0
    liveness = _manual_liveness(liveness_timeout(), lambda: now)
    batch_duration = 100.0
    indexer_config = MemoryIndexerConfig(batch_size=2)
    paths: set[Path] = set()
    for i in range(10 * indexer_config.batch_size):
        note = tmp_path / f"note-{i:03d}.md"
        note.write_text(f"content {i}", encoding="utf-8")
        paths.add(note.resolve())

    class SlowProvider(FakeProvider):
        def embed_batch(self, texts: list[str]) -> np.ndarray:
            nonlocal now
            now += batch_duration
            assert liveness.is_alive(), "liveness stale during embed batch"
            assert liveness.stale_for() == batch_duration, "previous batch compounded with embed"
            return super().embed_batch(texts)

    backend = _RecordingBackend()
    provider = SlowProvider()
    daemon.process_paths(
        backend, paths, provider, liveness, root=watched_root, config=indexer_config
    )

    assert now > liveness_timeout()
    assert provider.embed_batch_count == 10
    assert liveness.is_alive()
    assert set(backend.all_meta()) == {str(path) for path in paths}


@pytest.mark.parametrize("fail_last_batch", [False, True])
@pytest.mark.parametrize("provider_name", [PROVIDER, "embeddinggemma"])
def test_process_paths_beats_during_commit(
    tmp_path: Path,
    fail_last_batch: bool,
    provider_name: str,
    watched_root: Path,
) -> None:
    """The final embed, cleanup and upsert cannot share a beat-free interval."""
    now = 0.0
    liveness = _manual_liveness(
        config.liveness_timeout_seconds(provider_name, timeout_seconds=EMBED_TIMEOUT_S), lambda: now
    )
    durations = {"delete_stale_rows": 5.0, "upsert_many": 300.0}

    def spend(op: str) -> None:
        nonlocal now
        now += durations[op]
        assert liveness.is_alive(), f"liveness stale during {op}"
        assert liveness.stale_for() == durations[op], f"previous call compounded with {op}"

    class SlowFinalProvider(FakeProvider):
        def embed_batch(self, texts: list[str]) -> np.ndarray:
            nonlocal now
            now += factory.worst_case_batch_seconds(provider_name, timeout_seconds=EMBED_TIMEOUT_S)
            assert liveness.is_alive()
            if fail_last_batch and self.embed_batch_count == 1:
                raise EmbeddingAPIError("last batch failed")
            return super().embed_batch(texts)

    indexer_config = MemoryIndexerConfig(batch_size=1)
    paths = {tmp_path / name for name in ("a.md", "b.md")}
    for path in paths:
        path.write_text("body")
    backend = _RecordingBackend(spend)
    provider = SlowFinalProvider()
    if fail_last_batch:
        with pytest.raises(EmbeddingAPIError, match="last batch failed"):
            daemon.process_paths(
                backend, paths, provider, liveness, root=watched_root, config=indexer_config
            )
        assert set(backend.all_meta()) == {str(tmp_path / "a.md")}
    else:
        daemon.process_paths(
            backend, paths, provider, liveness, root=watched_root, config=indexer_config
        )
        assert set(backend.all_meta()) == {str(path) for path in paths}
    assert backend.calls == ["delete_stale_rows", "upsert_many"]


def test_process_paths_beats_per_delete(tmp_path: Path, *, watched_root: Path) -> None:
    """A delete-only drain batch may exceed the ceiling while each delete is timely."""
    now = 0.0
    liveness = _manual_liveness(liveness_timeout(), lambda: now)

    def spend(op: str) -> None:
        nonlocal now
        assert op == "delete"
        now += 3.0
        assert liveness.is_alive(), "liveness stale during accumulated deletes"

    backend = _RecordingBackend(spend)
    paths = {tmp_path / f"deleted-{i}.md" for i in range(400)}
    backend.rows = {(str(path), "body", 0): (0.0, "old", FINGERPRINT) for path in paths}
    provider = FakeProvider()
    daemon.process_paths(
        backend, paths, provider, liveness, root=watched_root, config=MemoryIndexerConfig()
    )
    assert now > liveness_timeout()
    assert backend.all_meta() == {}
    assert backend.calls == ["delete"] * len(paths)
    assert provider.embed_batch_count == 0


def test_factory_worst_case_registry_complete() -> None:
    for name in factory.PROVIDERS:
        budget = factory.worst_case_batch_seconds(name, timeout_seconds=EMBED_TIMEOUT_S)
        assert budget > 0, name
        assert config.liveness_timeout_seconds(name, timeout_seconds=EMBED_TIMEOUT_S) > budget, name

    unknown = "unknown-provider"
    with pytest.raises(ValueError, match="unknown embedding provider") as provider_error:
        factory.get_provider(
            unknown,
            catalog=build_model_catalog(home=meiva_home()),
            timeout_s=EMBED_TIMEOUT_S,
            api_key=None,
        )
    with pytest.raises(ValueError) as budget_error:
        factory.worst_case_batch_seconds(unknown, timeout_seconds=EMBED_TIMEOUT_S)
    assert str(budget_error.value) == str(provider_error.value)


def test_cold_start_reconcile_beats_liveness_across_chunks(
    tmp_path: Path, store_backend: StoreBackend
) -> None:
    """Regression (watchdog kill loop, 2026-09-19): the cold-start rebuild
    works the dirty set in file-granular chunks, beating liveness between them.
    Before the fix, a rebuild lasting longer than the liveness ceiling kept
    /healthz at 'loop: stale', so the watchdog restarted the daemon
    mid-rebuild and the rebuild restarted from zero every ~3 minutes — it
    never converged."""
    root = tmp_path / "watched"
    root.mkdir()
    files: list[Path] = []
    for i in range(7):
        note = root / f"note-{i}.md"
        note.write_text(f"content {i}")
        files.append(note)
    indexer_config = MemoryIndexerConfig(reconcile_chunk_paths=2)  # 7 files -> 4 chunks

    class SlowProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__()
            self.batch_sizes: list[int] = []

        def embed_batch(self, texts: list[str]) -> np.ndarray:
            self.batch_sizes.append(len(texts))
            time.sleep(0.2)
            return super().embed_batch(texts)

    provider = SlowProvider()
    liveness = Liveness(0.4)
    started = time.monotonic()
    assert (
        daemon.reconcile(
            store_backend,
            provider,
            liveness,
            config=indexer_config,
            status=daemon.PatrolStatus(retry_limit=indexer_config.retry_limit),
            root=root,
        )
        is True
    )
    elapsed = time.monotonic() - started

    # meiva:note
    # Chunked by file: one embed call per chunk, every file indexed once.
    assert provider.batch_sizes == [2, 2, 2, 1]
    assert set(store_backend.all_meta()) == {str(note) for note in files}
    # The rebuild outlived the liveness ceiling (4 chunks ≥ 0.8s > 0.4s)...
    assert elapsed > 0.4
    # ...yet the beats between chunks kept /healthz from ever reading stale.
    assert liveness.is_alive()
    assert liveness.stale_for() < 0.4


# ── chunk splitting (recall-v2: description + body chunks) ───────────────


def long_note(paragraphs: int) -> str:
    return "---\ntype: Memory\ndescription: hand off to 402\n---\n\n" + "\n\n".join(
        f"paragraph-{i} " + "word " * 80 for i in range(paragraphs)
    )
