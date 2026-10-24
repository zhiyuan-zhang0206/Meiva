# meiva:note
# Backend startup against a live memory-search service: the retry loop meets a real listener.

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

import pytest

from base.clock.tests.manual_timers import ManualTimers
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from services.derived.memory_indexer import daemon
from services.derived.memory_indexer.tests.fake_provider import DIM, FINGERPRINT, FakeProvider
from services.derived.memory_search.tests.search_service import free_port, serve_memory_search


# meiva:note
# Timers whose first back-off sleep is when the service starts listening.
class _ServiceComesUpWhileWaiting(ManualTimers):
    def __init__(self, stack: ExitStack, data_file: Path, port: int) -> None:
        super().__init__()
        self._stack = stack
        self._data_file = data_file
        self._port = port
        self._started = False

    async def asleep(self, seconds: float) -> None:
        if not self._started:
            self._started = True
            self._stack.enter_context(
                serve_memory_search(
                    self._data_file, dim=DIM, fingerprint=FINGERPRINT, port=self._port
                )
            )
        await super().asleep(seconds)


async def test_a_live_service_connects_without_waiting(
    tmp_path: Path, *, database_gate: ProcessDbGate
) -> None:
    timers = ManualTimers()
    with serve_memory_search(tmp_path / "vectors.npz", dim=DIM, fingerprint=FINGERPRINT) as uri:
        backend = await daemon.connect_backend_with_retry(
            Database.from_settings(gate=database_gate),
            FakeProvider(),
            name="numpy",
            uri=uri,
            timers=timers,
        )
        try:
            assert backend.name == "numpy"
            assert backend.all_meta() == {}
        finally:
            backend.close()
    assert timers.sleeps == []


async def test_connection_refusals_are_retried_until_the_service_is_up(
    tmp_path: Path, *, database_gate: ProcessDbGate
) -> None:
    port = free_port()
    with ExitStack() as stack:
        timers = _ServiceComesUpWhileWaiting(stack, tmp_path / "vectors.npz", port)
        backend = await daemon.connect_backend_with_retry(
            Database.from_settings(gate=database_gate),
            FakeProvider(),
            name="numpy",
            uri=f"http://127.0.0.1:{port}",
            timers=timers,
        )
        try:
            assert backend.all_meta() == {}
        finally:
            backend.close()
    assert timers.sleeps == [2.0]


async def test_a_service_that_never_comes_up_ends_in_one_actionable_error(
    *, database_gate: ProcessDbGate
) -> None:
    timers = ManualTimers()
    with pytest.raises(RuntimeError, match=r"numpy backend unreachable after 5\.0s") as failure:
        await daemon.connect_backend_with_retry(
            Database.from_settings(gate=database_gate),
            FakeProvider(),
            name="numpy",
            uri=f"http://127.0.0.1:{free_port()}",
            timers=timers,
            deadline_s=5.0,
            probe_message="start the memory-search service",
        )
    assert "start the memory-search service" in str(failure.value)
    assert timers.sleeps == [2.0, 2.0, 2.0]


async def test_a_configuration_error_is_not_retried(*, database_gate: ProcessDbGate) -> None:
    timers = ManualTimers()
    with pytest.raises(ValueError, match="unknown memory search backend 'nope'"):
        await daemon.connect_backend_with_retry(
            Database.from_settings(gate=database_gate),
            FakeProvider(),
            name="nope",
            uri="http://127.0.0.1:1",
            timers=timers,
        )
    assert timers.sleeps == []
