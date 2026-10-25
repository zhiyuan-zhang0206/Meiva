from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from base.clock import SYSTEM_TIMERS, Clock, Timers
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import ConfigBoot
from base.daemon.loop_health import LivenessGroup, LoopProgress
from base.db import Database
from base.db.tests.fakes import patch_database
from base.native_process.loaded_commit import LoadedCommit
from services.wake.heartbeat import completion_digest, daemon
from services.wake.heartbeat.loops import DispatchRound, LivenessRound


def _patch_run(
    monkeypatch: pytest.MonkeyPatch,
    loops: dict[str, Callable[..., Any]],
    events: list[str],
    seen: dict[str, object],
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    class _Pool:
        def close(self) -> None:
            events.append("pool")

    async def fake_start(
        _name: str, _port: int, *, liveness: LivenessGroup, image: LoadedCommit
    ) -> object:
        seen["image"] = image
        seen["trackers"] = sorted(liveness.snapshot())
        return object()

    async def fake_stop(_server: object) -> None:
        events.append("health")

    monkeypatch.setattr(daemon, "start_health_server", fake_start)
    monkeypatch.setattr(daemon, "stop_health_server", fake_stop)
    patch_database(monkeypatch, pool=_Pool)

    async def dispatch_loop(
        round: DispatchRound, progress: LoopProgress, *, timers: Timers
    ) -> None:
        await loops["dispatch"](round, progress)

    async def liveness_loop(
        round: LivenessRound, progress: LoopProgress, *, timers: Timers
    ) -> None:
        await loops["liveness"](round, progress)

    monkeypatch.setattr(daemon, "dispatch_loop", dispatch_loop)
    monkeypatch.setattr(daemon, "liveness_loop", liveness_loop)

    async def digest_loop(
        digest: completion_digest.CompletionDigestRound,
        progress: LoopProgress,
    ) -> None:
        assert digest.machine is machine_identity
        await loops["digest"](digest.pool, digest.db, digest.bus, progress)

    monkeypatch.setattr(daemon.completion_digest, "completion_digest_loop", digest_loop)


def test_every_loop_gets_its_own_progress_tracker(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    *,
    process_config: ConfigBoot,
    tmp_path: Path,
    machine_identity: MachineIdentityReader,
) -> None:
    received: dict[str, LoopProgress] = {}

    def loop(name: str) -> Callable[..., Any]:
        async def run_loop(*args: object) -> None:
            received[name] = cast(LoopProgress, args[-1])

        return run_loop

    seen: dict[str, object] = {}
    _patch_run(
        monkeypatch,
        {"dispatch": loop("dispatch"), "liveness": loop("liveness"), "digest": loop("digest")},
        [],
        seen,
        machine_identity=machine_identity,
    )

    asyncio.run(
        asyncio.wait_for(
            daemon.run(
                process_config=process_config,
                runtime=tmp_path / "run",
                database=lambda: database,
                image=LoadedCommit(Path(), None),
                machine=machine_identity,
                clock=Clock.from_settings(),
            ),
            timeout=5.0,
        )
    )

    assert seen["image"] == LoadedCommit(Path(), None)
    assert seen["trackers"] == ["completion_digest", "dispatch", "liveness"]
    assert len({id(progress) for progress in received.values()}) == 3


@pytest.mark.parametrize("crashing", ["dispatch", "liveness", "digest"])
def test_a_crashing_loop_cancels_its_siblings_and_ends_the_service(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    crashing: str,
    *,
    process_config: ConfigBoot,
    tmp_path: Path,
    machine_identity: MachineIdentityReader,
) -> None:
    cancelled: list[str] = []
    events: list[str] = []

    def loop(name: str) -> Callable[..., Any]:
        async def run_loop(*_args: object) -> None:
            if name == crashing:
                await asyncio.sleep(0.01)
                raise RuntimeError(f"{name} crashed")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(name)
                raise

        return run_loop

    _patch_run(
        monkeypatch,
        {"dispatch": loop("dispatch"), "liveness": loop("liveness"), "digest": loop("digest")},
        events,
        {},
        machine_identity=machine_identity,
    )

    with pytest.raises(ExceptionGroup) as raised:
        asyncio.run(
            asyncio.wait_for(
                daemon.run(
                    process_config=process_config,
                    runtime=tmp_path / "run",
                    database=lambda: database,
                    image=LoadedCommit(Path(), None),
                    machine=machine_identity,
                    clock=Clock.from_settings(),
                ),
                timeout=5.0,
            )
        )

    assert [str(exc) for exc in raised.value.exceptions] == [f"{crashing} crashed"]
    assert sorted(cancelled) == sorted({"dispatch", "liveness", "digest"} - {crashing})
    assert sorted(events) == ["health", "pool"]
    assert not (tmp_path / "run" / "heartbeat.pid").exists()


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
