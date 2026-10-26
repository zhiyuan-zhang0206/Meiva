from __future__ import annotations

import asyncio
import json
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.clock import SYSTEM_TIMERS, Clock, ClockConfig, Timers
from base.clock.tests.manual_timers import ManualTimers
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import ConfigBoot
from base.daemon.loop_health import LivenessGroup, LoopProgress
from base.daemon.round_loop import RoundFailureLimitError
from base.db import Database
from base.host.env import port_lease
from base.native_process.loaded_commit import LoadedCommit
from services.wake.heartbeat import completion_digest, daemon


def _free_port() -> int:
    return port_lease.lease_ports(1)[0]


def _port_refuses(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


async def _healthz(port: int) -> tuple[int, dict[str, Any]]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    headers, body = response.split(b"\r\n\r\n", 1)
    return int(headers.split()[1]), json.loads(body)


def _clock(*, timers: Timers = SYSTEM_TIMERS) -> Clock:
    return Clock(
        ClockConfig(
            timezone="UTC",
            authoritative_timezone="UTC",
            message_timestamp_weekday=False,
        ),
        timers=timers,
    )


class _FakeDispatchRound:
    interval_s = 0.01

    def __init__(self, on_round: Callable[[], None]) -> None:
        self._on_round = on_round

    def run_round(self) -> None:
        self._on_round()


class _FakeLivenessRound:
    def __init__(self, on_round: Callable[[], None]) -> None:
        self._on_round = on_round

    async def run_round(self) -> None:
        self._on_round()


class _FakeDigestRound:
    interval_s = 0.01

    def __init__(self, on_round: Callable[[], None]) -> None:
        self._on_round = on_round

    async def run_round(self) -> None:
        self._on_round()


async def test_every_loop_gets_its_own_progress_tracker(
    database: Database,
    *,
    process_config: ConfigBoot,
    tmp_path: Path,
    machine_identity: MachineIdentityReader,
) -> None:
    port = _free_port()
    process_config.set_field("heartbeat_health_port", port)
    image = LoadedCommit(tmp_path, "captured-heartbeat")
    runtime = tmp_path / "run"

    task = asyncio.create_task(
        daemon.run(
            process_config=process_config,
            runtime=runtime,
            database=lambda: database,
            image=image,
            machine=machine_identity,
            clock=_clock(),
        )
    )
    try:
        deadline = time.monotonic() + 5.0
        while True:
            try:
                status, payload = await _healthz(port)
                break
            except OSError:
                assert time.monotonic() < deadline, "the health server never came up"
                await asyncio.sleep(0.02)
        assert status == 200
        assert payload["sha"] == "captured-heartbeat"
        assert sorted(payload["loops"]) == ["completion_digest", "dispatch", "liveness"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert _port_refuses(port)
    assert not (runtime / "heartbeat.pid").exists()


@pytest.mark.parametrize("crashing", ["dispatch", "liveness", "digest"])
async def test_a_crashing_loop_cancels_its_siblings_and_ends_the_service(
    crashing: str,
) -> None:
    timers = ManualTimers()
    liveness = LivenessGroup(timers=timers)
    before = asyncio.all_tasks()

    def maybe_crash(name: str) -> None:
        if name == crashing:
            raise RuntimeError(f"{name} crashed")

    with pytest.raises(ExceptionGroup) as raised:
        await asyncio.wait_for(
            daemon.run_loops(
                _FakeDispatchRound(lambda: maybe_crash("dispatch")),
                _FakeLivenessRound(lambda: maybe_crash("liveness")),
                _FakeDigestRound(lambda: maybe_crash("digest")),
                liveness,
                timers=timers,
            ),
            timeout=5.0,
        )

    assert len(raised.value.exceptions) == 1
    exc = raised.value.exceptions[0]
    if crashing == "digest":
        assert isinstance(exc, RuntimeError)
        assert str(exc) == "digest crashed"
    else:
        assert isinstance(exc, RoundFailureLimitError)
        assert str(exc.__cause__) == f"{crashing} crashed"
    assert set(liveness.snapshot()) == {"dispatch", "liveness", "completion_digest"}
    assert asyncio.all_tasks() == before, "a sibling loop was left running"


async def test_the_digest_loop_flushes_at_once_then_paces() -> None:
    flushed = asyncio.Event()
    rounds: list[int] = []

    class Flush:
        interval_s = completion_digest.FLUSH_INTERVAL_S

        async def run_round(self) -> None:
            rounds.append(1)
            flushed.set()

    task = asyncio.create_task(
        completion_digest.completion_digest_loop(
            Flush(), LoopProgress("digest", 300.0, timers=SYSTEM_TIMERS)
        )
    )
    try:
        await asyncio.wait_for(flushed.wait(), timeout=5)
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert rounds == [1]


async def test_a_failing_flush_ends_the_digest_loop() -> None:
    class Flush:
        interval_s = completion_digest.FLUSH_INTERVAL_S

        async def run_round(self) -> None:
            raise RuntimeError("digest table unreadable")

    with pytest.raises(RuntimeError, match="digest table unreadable"):
        await completion_digest.completion_digest_loop(
            Flush(), LoopProgress("digest", 300.0, timers=SYSTEM_TIMERS)
        )
