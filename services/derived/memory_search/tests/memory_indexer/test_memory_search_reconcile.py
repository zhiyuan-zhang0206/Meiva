# meiva:note
# The reconcile tool's shell over real resources.
#
# Two live memory-search services stand in for the two backends, a pseudo-terminal feeds the
# confirmation prompt of a child process, and the empty-pool exit runs in a child process over a
# throwaway home. The tool's decisions are pinned by value in
# `services/derived/memory_indexer/tests/test_reconcile_plan.py`.

from __future__ import annotations

import io
import os
import pty
import subprocess
import sys
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path

import numpy as np
import pytest

from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from services.derived.memory_indexer import memory_search_reconcile as reconcile
from services.derived.memory_indexer.backends.numpy import NumPyBackend
from services.derived.memory_indexer.memory_search_reconcile import ReconcileArguments
from services.derived.memory_indexer.tests.fake_provider import DIM, FINGERPRINT, FakeProvider
from services.derived.memory_search.tests.search_service import free_port, serve_memory_search

_REPO_ROOT = Path(__file__).resolve().parents[5]
_QUERY = "what do we know"
_ONES = np.ones(DIM, dtype=np.float32)
_PROMPT = "This may rebuild backend storage. Type exact 'yes' to continue: "


class _OnesProvider(FakeProvider):
    def embed_query(self, text: str) -> np.ndarray:
        return _ONES


def _arguments(*, b: str = "shadow", allow_write: bool = False) -> ReconcileArguments:
    return ReconcileArguments(a="numpy", b=b, limit=50, k=5, allow_write=allow_write)


def _seed(uri: str, rows: dict[str, np.ndarray]) -> None:
    writer = NumPyBackend(uri=uri)
    writer.connect()
    try:
        for path, vector in rows.items():
            writer.upsert(path, 1.0, "hash", vector, kind="body", chunk_idx=0)
    finally:
        writer.close()


def _run(
    args: ReconcileArguments, backend_a: NumPyBackend, backend_b: NumPyBackend
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = reconcile.run_comparison(
        args,
        provider=_OnesProvider(),
        backend_a=backend_a,
        backend_b=backend_b,
        queries=[_QUERY],
        out=out,
        err=err,
    )
    return status, out.getvalue(), err.getvalue()


def _assert_closed(*backends: NumPyBackend) -> None:
    for backend in backends:
        with pytest.raises(RuntimeError, match="not connected"):
            backend.all_meta()


@pytest.fixture
def services(tmp_path: Path) -> Iterator[tuple[str, str]]:
    """Two live memory-search services, one per side of the comparison."""
    with ExitStack() as stack:
        first = stack.enter_context(
            serve_memory_search(tmp_path / "a" / "vectors.npz", dim=DIM, fingerprint=FINGERPRINT)
        )
        second = stack.enter_context(
            serve_memory_search(tmp_path / "b" / "vectors.npz", dim=DIM, fingerprint=FINGERPRINT)
        )
        yield first, second


def test_identical_orderings_report_a_clean_summary(services: tuple[str, str]) -> None:
    for uri in services:
        _seed(uri, {"/a.md": _ONES, "/b.md": -_ONES})
    backend_a = NumPyBackend(uri=services[0], readonly=True)
    backend_b = NumPyBackend(uri=services[1], readonly=True)

    status, out, err = _run(_arguments(), backend_a, backend_b)

    assert (status, err) == (0, "")
    assert out == "summary: 1/1 queries identical (numpy vs shadow, k=5)\n"
    _assert_closed(backend_a, backend_b)


def test_differing_orderings_are_listed_and_counted(services: tuple[str, str]) -> None:
    _seed(services[0], {"/a.md": _ONES, "/b.md": -_ONES})
    _seed(services[1], {"/a.md": -_ONES, "/b.md": _ONES})
    backend_a = NumPyBackend(uri=services[0], readonly=True)
    backend_b = NumPyBackend(uri=services[1], readonly=True)

    status, out, err = _run(_arguments(), backend_a, backend_b)

    assert (status, err) == (0, "")
    assert out.splitlines() == [
        f"[1] {_QUERY!r}",
        f"    {'numpy':>10}: ['/a.md', '/b.md']",
        f"    {'shadow':>10}: ['/b.md', '/a.md']",
        "summary: 0/1 queries identical (numpy vs shadow, k=5)",
    ]
    _assert_closed(backend_a, backend_b)


def test_different_row_sets_stop_the_comparison(services: tuple[str, str]) -> None:
    _seed(services[0], {"/a.md": _ONES, "/b.md": -_ONES})
    _seed(services[1], {"/a.md": _ONES})
    backend_a = NumPyBackend(uri=services[0], readonly=True)
    backend_b = NumPyBackend(uri=services[1], readonly=True)

    status, out, err = _run(_arguments(), backend_a, backend_b)

    assert (status, out) == (2, "")
    assert "row sets differ: numpy=2 paths, shadow=1; only in numpy: ['/b.md']" in err
    _assert_closed(backend_a, backend_b)


def test_a_read_only_connection_failure_is_reported_without_traceback(
    services: tuple[str, str],
) -> None:
    backend_a = NumPyBackend(uri=services[0], readonly=True)
    backend_b = NumPyBackend(uri=f"http://127.0.0.1:{free_port()}", readonly=True)

    status, out, err = _run(_arguments(), backend_a, backend_b)

    assert (status, out) == (1, "")
    assert err.startswith("read-only backend connection refused: ")
    assert "next patrol (15 minutes by default) repairs the backend" in err
    assert "Traceback" not in err
    _assert_closed(backend_a, backend_b)


def test_a_writable_connection_failure_is_reported_without_traceback(
    services: tuple[str, str],
) -> None:
    backend_a = NumPyBackend(uri=f"http://127.0.0.1:{free_port()}")
    backend_b = NumPyBackend(uri=services[1])

    status, out, err = _run(_arguments(allow_write=True), backend_a, backend_b)

    assert (status, out) == (1, "")
    assert err.startswith("backend connection failed: ")
    assert "Traceback" not in err
    _assert_closed(backend_a, backend_b)


def test_backends_are_read_only_unless_a_write_was_confirmed(
    services: tuple[str, str], *, database_gate: ProcessDbGate
) -> None:
    database = Database.from_settings(gate=database_gate)
    for allow_write in (False, True):
        backends = reconcile.build_backends(
            _arguments(b="numpy", allow_write=allow_write),
            database=database,
            provider=_OnesProvider(),
            uri=services[0],
        )
        for backend in backends:
            backend.connect()
            try:
                if allow_write:
                    backend.upsert("/w.md", 1.0, "hash", _ONES, kind="body", chunk_idx=0)
                    assert "/w.md" in backend.all_meta()
                else:
                    with pytest.raises(RuntimeError, match="read-only"):
                        backend.upsert("/w.md", 1.0, "hash", _ONES, kind="body", chunk_idx=0)
            finally:
                backend.close()


def test_an_unknown_backend_name_fails_fast(
    services: tuple[str, str], *, database_gate: ProcessDbGate
) -> None:
    with pytest.raises(ValueError, match="unknown memory search backend 'nope'"):
        reconcile.build_backends(
            _arguments(b="nope"),
            database=Database.from_settings(gate=database_gate),
            provider=_OnesProvider(),
            uri=services[0],
        )


# meiva:note
# The environment of a throwaway unit that serves the gateway and owns an empty memory pool.
def _unit_home(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(
        "MEIVA_MACHINE_NAME=test-box\n"
        "MEIVA_MACHINE_SERVE_GATEWAY=true\n"
        "MEIVA_MACHINE_SERVE_AGENT_RUNNER=true\n"
        "MEIVA_DB_URL=postgresql://unprovisioned@127.0.0.1:1/unprovisioned\n"
        "MEIVA_REDIS_URL=redis://127.0.0.1:1/0\n"
        "MEIVA_CLUSTER_SECRET=test-cluster-secret\n"
        "MEIVA_OS_JOBS_ENABLED=false\n"
        "MEIVA_TELEMETRY_OTLP_ENABLED=false\n"
        "MEIVA_PGBOUNCER_ENABLED=false\n"
        "MEIVA_TRANSPORT_ENCRYPTION=overlay\n"
    )
    env = {**os.environ, "MEIVA_HOME": str(home)}
    env.pop("MEIVA_PROCESS_PROFILE", None)
    env.pop("MEIVA_AGENT_ID", None)
    return env


def _start(env: dict[str, str], *flags: str, stdin: int) -> subprocess.Popen[str]:
    # meiva:note
    # A fixed interpreter and repository-owned module: no shell, no user input.
    return subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "services.derived.memory_indexer.memory_search_reconcile",
            "--a",
            "numpy",
            "--b",
            "pgvector",
            *flags,
        ],
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=_REPO_ROOT,
        env=env,
    )


def test_an_empty_query_pool_stops_before_any_backend_is_built(tmp_path: Path) -> None:
    child = _start(_unit_home(tmp_path), stdin=subprocess.DEVNULL)
    out, err = child.communicate(timeout=120)

    assert child.returncode == 1
    assert (out, err) == ("", "no query texts found in the memory pool\n")


def test_allow_write_without_a_terminal_connects_nothing(tmp_path: Path) -> None:
    child = _start(_unit_home(tmp_path), "--allow-write", stdin=subprocess.DEVNULL)
    out, err = child.communicate(timeout=120)

    assert child.returncode == 1
    assert (out, err) == (
        "",
        "--allow-write requires an interactive terminal; no backend connected\n",
    )


@pytest.mark.parametrize(
    ("typed", "refusal"),
    [
        (b"yes \n", "--allow-write was not confirmed; no backend connected\n"),
        (b"\x04", "--allow-write confirmation was unavailable (EOF); no backend connected\n"),
    ],
)
def test_allow_write_at_a_terminal_needs_the_exact_word(
    tmp_path: Path, typed: bytes, refusal: str
) -> None:
    terminal, slave = pty.openpty()
    try:
        child = _start(_unit_home(tmp_path), "--allow-write", stdin=slave)
        os.close(slave)
        os.write(terminal, typed)
        out, err = child.communicate(timeout=120)
    finally:
        os.close(terminal)

    assert child.returncode == 1
    assert (out, err) == (_PROMPT, refusal)
