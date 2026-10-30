"""A failed background loop exits the host, and cannot skip turn drain or ownership release.

The loops share one `TaskGroup` with the wake dispatcher: a loop that raises
cancels the dispatcher and its siblings, and `run` raises so `meiva-root` restarts
the process after the turns drain.

The sweep's bounded-exit regression (task #4224) rides the shared child-process
harness: production ``main()`` must exit within a small bound of SIGTERM even
with a default-executor job mid-flight.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
from asyncio.constants import THREAD_JOIN_TIMEOUT
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, cast

import pytest

from base.agents.observation.turn_progress import TurnProgress
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import get_field
from base.daemon.endpoints import ServiceEndpoints
from base.db.code_version_gate import ProcessDbGate
from base.host.tests.daemon_shutdown import (
    EXIT_BOUND_S,
    KILL_SLACK_S,
    spawn_child,
)
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import run_dir
from base.telemetry import process_name
from services.agent_runner.agent_host.lifecycle.graceful_exit import OwedRestart


def _describe(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        members = cast("BaseExceptionGroup[BaseException]", exc).exceptions
        return f"ExceptionGroup[{','.join(type(e).__name__ for e in members)}]"
    return type(exc).__name__


async def _beat_for_shutdown(
    events: list[str], failure: str, *_args: object, **_kwargs: object
) -> Exception | None:
    try:
        events.append("beat_started")
        if failure in {"heartbeat", "heartbeat_boot"}:
            events.append("beat_failed")
            raise RuntimeError("heartbeat defect")
        await asyncio.Event().wait()
    finally:
        events.append("beat_stopped")
    return None


async def _dispatch_for_shutdown(events: list[str], failure: str) -> None:
    if failure == "heartbeat":
        for _ in range(4):
            await asyncio.sleep(0)
        events.append("dispatcher_continued")
        raise ValueError("dispatcher stopped independently")
    if failure == "dispatcher_returns":
        await asyncio.sleep(0.01)  # let the sibling loop start before the return
        return
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        events.append("dispatcher_cancelled")
        raise


async def _sigterm_and_keep_serving() -> None:
    signal.raise_signal(signal.SIGTERM)
    await asyncio.Event().wait()


def _assert_boot_primary(exc: BaseException, failure: str) -> None:
    if failure == "heartbeat_boot":
        assert isinstance(exc, asyncio.CancelledError)
        assert any("heartbeat defect" in note for note in exc.__notes__)


def exercise_shutdown(failure: str, home: Path) -> None:
    """Run real signal/asyncio unwinding in a disposable child interpreter."""
    image = LoadedCommit.capture()
    version = CodeVersion(image)
    _ = ProcessDbGate(version=version.get, process=process_name(), exempt=False)
    _run_shutdown(failure, home=home)


async def _install_plugin_soon(root: Path) -> None:
    await asyncio.sleep(0.05)
    (root / "alpha").mkdir(parents=True, exist_ok=True)
    (root / "alpha" / "plugin.py").write_text("x = 1\n", encoding="utf-8")


async def _settle_for_shutdown(events: list[str], failure: str) -> list[int]:
    if failure == "heartbeat_boot":
        for _ in range(4):
            await asyncio.sleep(0)
        events.append("boot_continued")
        raise ValueError("boot settlement stopped independently")
    await asyncio.sleep(0)
    assert "beat_started" in events
    events.append("boot_settled")
    return []


class _SamplingRecorder:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def close(self) -> bool:
        self._events.append("sampling_stopped")
        return True


class _InstallationHandle:
    def __init__(self, events: list[str]) -> None:
        self.sampling = _SamplingRecorder(events)


class _PoolRecorder:
    def __init__(self, events: list[str], *, pidfile: Path, record_close: bool) -> None:
        self._events = events
        self._pidfile = pidfile
        self._record_close = record_close

    async def close(self) -> None:
        assert self._pidfile.exists()
        if self._record_close:
            await asyncio.sleep(0)
            self._events.append("pools_closed")


class _HealthRecorder:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)
        self._events.append("health_closed")


class _HostRecorder:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.resources_joined = True
        self.turn_progress = TurnProgress()

    async def aclose(self, *, resource_deadline: float | None = None) -> None:
        _ = resource_deadline
        await asyncio.sleep(0)
        self._events.append("owner_released")


class _SchedulerRecorder:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def task_for(self, agent_id: int) -> asyncio.Task[None] | None:
        _ = agent_id
        return None

    def stop_admission(self) -> frozenset[int]:
        return frozenset()

    async def aclose(self) -> None:
        await asyncio.sleep(0)
        self._events.append("turns_drained")


async def _noop_restart(agent_id: int) -> OwedRestart:
    _ = agent_id
    return OwedRestart(0, written=False)


async def _open_health_recorder(events: list[str]) -> asyncio.Server:
    return cast(asyncio.Server, _HealthRecorder(events))


def _run_shutdown(failure: str, *, home: Path) -> None:
    """Run real signal/asyncio unwinding in a disposable child interpreter."""
    from services.agent_runner.agent_host import daemon

    events: list[str] = []

    async def record(name: str, **_kwargs: object) -> None:
        await asyncio.sleep(0)
        events.append(name)

    async def background(*_args: object) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            await record("background_joined")

    async def fail() -> None:
        await asyncio.sleep(0)
        if failure == "signal":
            await _sigterm_and_keep_serving()
        raise ValueError("background failed")

    def loops(plugins_root: Path) -> dict[str, Coroutine[object, object, None]]:
        if failure == "plugin":
            # meiva:note
            # The watcher sees a plugin appear under its directory and raises the restart signal.
            return {
                "plugins_watch": daemon.watch_plugins_for_restart(plugins_root, interval_s=0.01),
                "installer": _install_plugin_soon(plugins_root),
                "sibling": background(),
            }
        failing = {} if failure in {"dispatcher_returns", "heartbeat"} else {"failed": fail()}
        return {**failing, "sibling": background()}

    async def serve_dispatch(plugins_root: Path) -> None:
        await daemon.run_dispatcher_group(
            loops(plugins_root),
            _dispatch_for_shutdown(events, failure),
        )

    # meiva:note
    # The row the daemon records: this child's own reader and the home its parent handed it, so a
    # boot here never depends on the environment the parent happened to export.
    pidfile = (
        ServiceEndpoints.from_fields(get_field, runtime=run_dir(home)).of("agent_host").pidfile
    )
    assert daemon.acquire_pidfile(
        pidfile, "services.agent_runner.agent_host.daemon", pid=os.getpid()
    )
    workload = _PoolRecorder(events, pidfile=pidfile, record_close=True)
    control = _PoolRecorder(events, pidfile=pidfile, record_close=False)
    installation = _InstallationHandle(events)
    host = _HostRecorder(events)
    scheduler = _SchedulerRecorder(events)
    daemon.install_graceful_shutdown("agent_host_test")
    try:
        try:
            asyncio.run(
                daemon.serve_host_lifecycle(
                    cast(Any, host),
                    cast(Any, scheduler),
                    cast(Any, workload),
                    cast(Any, control),
                    cast(Any, installation),
                    beat_coro=_beat_for_shutdown(events, failure),
                    settle_coro=_settle_for_shutdown(events, failure),
                    open_health=_open_health_recorder(events),
                    serve_dispatch=serve_dispatch(home / "plugins"),
                    write_restart=_noop_restart,
                    pidfile=pidfile,
                )
            )
        finally:
            if not pidfile.exists():
                events.append("pidfile_removed")
    except (
        KeyboardInterrupt,
        asyncio.CancelledError,
        ExceptionGroup,
        RuntimeError,
        ValueError,
    ) as exc:
        _assert_boot_primary(exc, failure)
        events.append(_describe(exc))
    print(json.dumps(events))  # noqa: T201 -- child result protocol


def _assert_heartbeat_order(events: list[str], failure: str) -> None:
    assert events.index("beat_started") < events.index("boot_settled")
    assert events.index("beat_stopped") < events.index("owner_released")
    assert events.index("sampling_stopped") < events.index("pools_closed")
    if failure in {"exception", "dispatcher_returns"}:
        assert events.index("turns_drained") < events.index("beat_stopped")
        assert events.index("turns_drained") < events.index("sampling_stopped")
    if failure == "heartbeat":
        assert "dispatcher_continued" not in events
        assert events.index("beat_failed") < events.index("pools_closed")


def _shutdown_child(failure: str, home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed test helper in this checkout
        [
            sys.executable,
            __file__,
            failure,
            str(home),
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )


# meiva:note
# A SIGTERM while the dispatcher serves (the plugin watcher raises one at itself) runs the
# graceful exit instead of a failure: `run` returns, with no exception, after the same drain
# and release.
# meiva:link services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md
@pytest.mark.parametrize(
    "failure,exception",
    [
        ("plugin", None),
        ("signal", None),
        ("exception", "ExceptionGroup[ValueError]"),
        ("dispatcher_returns", "ExceptionGroup[RuntimeError]"),
        ("heartbeat", "ExceptionGroup[RuntimeError]"),
    ],
)
def test_failed_background_still_drains_and_releases(
    failure: str, exception: str | None, unit_home: Path
) -> None:
    result = _shutdown_child(failure, unit_home)
    assert result.returncode == 0, result.stdout + result.stderr
    events = json.loads(result.stdout.splitlines()[-1])
    assert events.count("background_joined") == 1
    # A process interrupt lets asyncio cancel the heartbeat immediately. Both
    # restart and background-failure paths must still drain turns and release
    # ownership before closing their DB pools.
    ordered = [
        "turns_drained",
        "owner_released",
        "health_closed",
        "sampling_stopped",
        "pools_closed",
        "pidfile_removed",
        *([] if exception is None else [exception]),
    ]
    assert [
        event
        for event in events
        if event
        not in {
            "background_joined",
            "beat_stopped",
            "dispatcher_cancelled",
            "beat_failed",
            "beat_started",
            "boot_settled",
            "dispatcher_continued",
        }
    ] == ordered
    assert events.index("background_joined") < events.index("turns_drained")
    _assert_heartbeat_order(events, failure)
    if failure == "exception":
        # The crashed loop took the dispatcher down with it, before any drain.
        assert events.index("dispatcher_cancelled") < events.index("turns_drained")
        assert events.index("turns_drained") < events.index("beat_stopped")


def test_sigterm_bounded_exit_with_wedged_executor(tmp_path: Path) -> None:
    """SIGTERM exits within the bound while a wedged default-executor job stands."""
    # meiva:note
    # The exit bound only matters relative to the stall it separates itself from:
    # assert the relationship, not just the number.
    assert EXIT_BOUND_S + KILL_SLACK_S < THREAD_JOIN_TIMEOUT / 5
    child = spawn_child(
        tmp_path,
        command=[
            sys.executable,
            "-m",
            "services.agent_runner.agent_host.tests.runtime.shutdown.child_shutdown",
        ],
        label="agent-host",
    )
    try:
        child.terminate()
        child.wait_bounded_exit(what="wedged executor job")
        assert "[agent-host] interrupted, shutting down" in child.log_tail(), child.log_tail()
        assert "cleanup-ran" in child.markers(), (
            "the cancellation drain did not reach run()'s cleanup"
        )
    finally:
        child.close()


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_internal_sigterm_drains_async_cleanup_before_exit(
    tmp_path: Path, cleanup_failure: bool
) -> None:
    child = spawn_child(
        tmp_path,
        command=[
            sys.executable,
            "-m",
            "services.agent_runner.agent_host.tests.runtime.shutdown.child_shutdown",
        ],
        label="agent-host",
        internal_stop=True,
        cleanup_failure=cleanup_failure,
    )
    try:
        child.wait_bounded_exit(
            what="TaskGroup child requested SIGTERM with a wedged executor",
            expected_codes=(1,) if cleanup_failure else (0,),
        )
        markers = child.markers().splitlines()
        assert markers.index("cleanup-started") < markers.index("cleanup-ran")
        assert "[agent-host] interrupted, shutting down" in child.log_tail()
        if cleanup_failure:
            assert "async shutdown failed" in child.log_tail()
            assert "surrogate cleanup failure" in child.log_tail()
    finally:
        child.close()


def test_failed_heartbeat_during_boot_still_closes_pools_and_pidfile(unit_home: Path) -> None:
    result = _shutdown_child("heartbeat_boot", unit_home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == [
        "beat_started",
        "beat_failed",
        "beat_stopped",
        "sampling_stopped",
        "pools_closed",
        "pidfile_removed",
        "CancelledError",
    ]


if __name__ == "__main__":
    exercise_shutdown(sys.argv[1], Path(sys.argv[2]))
