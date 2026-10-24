from __future__ import annotations

import socket

from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from services.derived.memory_indexer.backends import probe


def _free_port() -> int:
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        return probe_socket.getsockname()[1]


def test_probe_numpy_unreachable_is_actionable(*, database_gate: ProcessDbGate) -> None:
    result = probe.probe_backend(
        "numpy",
        Database.from_settings(gate=database_gate),
        uri=f"http://127.0.0.1:{_free_port()}",
    )
    assert not result.fatal
    assert "memory_search service is not reachable" in (result.message or "")
    assert "MEIVA_MEMORY_SEARCH_BACKEND=numpy" in (result.message or "")


def test_probe_unknown_backend_is_fatal(*, database_gate: ProcessDbGate) -> None:
    result = probe.probe_backend(
        "qdrant", Database.from_settings(gate=database_gate), uri="http://127.0.0.1:1"
    )
    assert result.fatal
    assert "unknown memory search backend" in (result.message or "")
