from __future__ import annotations

import io
import os
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock, patch

import pytest

from base.cluster.machine import MachineRoles
from base.db import Database
from base.native_process import code_version
from base.native_process.loaded_commit import LoadedCommit
from services.derived.memory_indexer import memory_search_reconcile as reconcile
from services.derived.memory_indexer.backends.base import ChunkHit, MemorySearchBackend


@pytest.fixture(autouse=True)
def _restore_boot_environment() -> Iterator[None]:
    with patch.dict(os.environ):
        yield


class _FakeProvider:
    def __init__(
        self,
        name: str,
        *,
        catalog: object,
        timeout_reader: Callable[[], float],
        api_key_reader: Callable[[], str | None],
    ) -> None:
        self.catalog = catalog

    dim = 8
    fingerprint = "test:provider:dim=8"

    def embed_query(self, text: str) -> object:
        del text
        return object()


class _FakeBackend:
    def __init__(self, connect_error: Exception | None = None) -> None:
        self.connect_error = connect_error
        self.connected = False
        self.closed = False

    def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    def close(self) -> None:
        self.closed = True

    def all_meta(self) -> dict[str, tuple[float, str, str]]:
        return {}

    def search_topk(self, vector: object, k: int) -> list[ChunkHit]:
        del vector, k
        return []


class _TTYStdin:
    def isatty(self) -> bool:
        return True


def _one_query(limit: int, *, roles: MachineRoles) -> list[str]:
    del roles
    return [f"query-{limit}"]


def _confirmation_with_space(_prompt: str = "") -> str:
    return "yes "


def _exact_confirmation(_prompt: str = "") -> str:
    return "yes"


def _make_reconcile_deps(
    backends: list[_FakeBackend],
) -> tuple[reconcile.ReconcileDeps, list[tuple[str, int, str, bool]]]:
    calls: list[tuple[str, int, str, bool]] = []

    def _get_backend_named(
        name: str,
        *,
        database: object,
        dim: int,
        fingerprint: str,
        readonly: bool,
        uri_reader: Callable[[], str],
    ) -> MemorySearchBackend:
        del database, uri_reader
        calls.append((name, dim, fingerprint, readonly))
        return cast(MemorySearchBackend, backends.pop(0))

    deps = reconcile.ReconcileDeps(
        sample_queries_fn=_one_query,
        get_provider_fn=_FakeProvider,
        get_backend_named_fn=_get_backend_named,
    )
    return deps, calls


def test_default_run_connects_both_backends_readonly() -> None:
    backends = [_FakeBackend(), _FakeBackend()]
    deps, calls = _make_reconcile_deps(backends)

    assert reconcile.main(["--a", "numpy", "--b", "pgvector"], deps=deps) == 0
    assert calls == [
        ("numpy", 8, "test:provider:dim=8", True),
        ("pgvector", 8, "test:provider:dim=8", True),
    ]


def test_allow_write_without_exact_confirmation_never_connects_writable() -> None:
    backends = [_FakeBackend(), _FakeBackend()]
    deps, calls = _make_reconcile_deps(backends)
    deps = replace(deps, stdin=_TTYStdin(), input_fn=_confirmation_with_space)

    assert (
        reconcile.main(["--a", "numpy", "--b", "pgvector", "--allow-write"], deps=deps) == 1
    )
    assert calls == []


def test_allow_write_requires_a_terminal() -> None:
    backends = [_FakeBackend(), _FakeBackend()]
    deps, calls = _make_reconcile_deps(backends)
    deps = replace(deps, stdin=io.StringIO("yes\n"))

    assert (
        reconcile.main(["--a", "numpy", "--b", "pgvector", "--allow-write"], deps=deps) == 1
    )
    assert calls == []


def test_allow_write_with_exact_confirmation_connects_writable_and_runs_comparison() -> None:
    backend_a = _FakeBackend()
    backend_b = _FakeBackend()
    backends = [backend_a, backend_b]
    deps, calls = _make_reconcile_deps(backends)
    deps = replace(deps, stdin=_TTYStdin(), input_fn=_exact_confirmation)

    assert (
        reconcile.main(["--a", "numpy", "--b", "pgvector", "--allow-write"], deps=deps) == 0
    )
    assert [call[-1] for call in calls] == [False, False]
    assert backend_a.connected and backend_b.connected
    assert backend_a.closed and backend_b.closed


def test_writable_connection_failure_is_reported_without_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend_a = _FakeBackend(ValueError("numpy unavailable"))
    backend_b = _FakeBackend()
    deps, calls = _make_reconcile_deps([backend_a, backend_b])
    deps = replace(deps, stdin=_TTYStdin(), input_fn=_exact_confirmation)

    assert (
        reconcile.main(["--a", "numpy", "--b", "pgvector", "--allow-write"], deps=deps) == 1
    )

    err = capsys.readouterr().err
    assert "backend connection failed" in err
    assert "numpy unavailable" in err
    assert "Traceback" not in err
    assert [call[-1] for call in calls] == [False, False]
    assert backend_a.closed and backend_b.closed


def test_readonly_connection_mismatch_is_reported_without_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    backends = [_FakeBackend(RuntimeError("pgvector table schema mismatch")), _FakeBackend()]
    deps, _ = _make_reconcile_deps(backends)

    assert reconcile.main(["--a", "numpy", "--b", "pgvector"], deps=deps) == 1

    err = capsys.readouterr().err
    assert "schema mismatch" in err
    assert "next patrol (15 minutes by default) repairs the backend" in err
    assert "Traceback" not in err


def test_standalone_entry_shares_captured_gated_database_between_backends(
    tmp_path: Path,
) -> None:
    base_deps, _ = _make_reconcile_deps([_FakeBackend(), _FakeBackend()])
    backend = Mock(side_effect=base_deps.get_backend_named_fn)
    create = Mock(side_effect=Database)
    image = LoadedCommit(tmp_path, "captured-readonly-reconcile")
    capture = Mock(return_value=image)
    count = Mock(return_value=7)

    class _FakeCodeVersion(code_version.CodeVersion):
        def get(self) -> int:
            if self._version is None:
                self._version = count(self.loaded.source_root, self.loaded.sha)
            return self._version

    deps = replace(
        base_deps,
        get_backend_named_fn=backend,
        database_cls=create,
        capture_image=capture,
        code_version_cls=_FakeCodeVersion,
        process_name_fn=lambda: "readonly-probe",
    )

    assert reconcile.main(["--a", "numpy", "--b", "pgvector"], deps=deps) == 0
    capture.assert_called_once_with()
    create.assert_called_once()
    assert (
        backend.call_args_list[0].kwargs["database"] is backend.call_args_list[1].kwargs["database"]
    )
    assert all(call.kwargs["readonly"] for call in backend.call_args_list)
    count.assert_not_called()
    gate = create.call_args.kwargs["gate"]
    assert gate.application_name() == "meiva:readonly-probe:v7"
    count.assert_called_once_with(tmp_path, image.sha)


def test_empty_query_pool_returns_before_capture_or_database() -> None:
    capture = Mock(side_effect=AssertionError("no entry image needed without work"))
    create = Mock(side_effect=AssertionError("empty query pool must not dial"))

    def no_queries(limit: int, *, roles: MachineRoles) -> list[str]:
        return []

    deps = reconcile.ReconcileDeps(
        sample_queries_fn=no_queries,
        capture_image=capture,
        database_cls=create,
    )
    assert reconcile.main(["--a", "numpy", "--b", "pgvector"], deps=deps) == 1
    capture.assert_not_called()
    create.assert_not_called()
