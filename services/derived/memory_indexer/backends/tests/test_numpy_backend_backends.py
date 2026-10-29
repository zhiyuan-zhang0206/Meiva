from __future__ import annotations

from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.host.env import port_lease
from services.derived.memory_indexer.backends import probe


# meiva:note
# One port this session holds, from the host-wide lease: the number is refused to every
# other live holder on the host, so two workers — or two runs of one shard on one machine —
# cannot be given it.
def _free_port() -> int:
    return port_lease.lease_ports(1)[0]


def test_probe_numpy_unreachable_is_actionable(*, database_gate: ProcessDbGate) -> None:
    result = probe.probe_backend(
        "numpy",
        Database.from_settings(gate=database_gate),
        uri=f"http://127.0.0.1:{_free_port()}",
        fingerprint="test-space",
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
