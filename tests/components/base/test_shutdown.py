"""Common signal registration and real supervised-daemon cleanup."""

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from base.daemon import shutdown
from base.host.proc import run_bounded
from base.log.tests.log_records import LogRecords
from base.sessions.tests.poll_until import poll_until
from services.supervision.meiva_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.supervision.meiva_root.supervisor import Supervisor, SupervisorConfig


def test_cancel_and_drain_runs_cleanup_and_returns_only_ordinary_failures() -> None:
    cleaned: list[str] = []
    tasks: list[asyncio.Task[None]] = []

    async def cancelled() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append("cancelled")

    async def failed() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append("failed")
            raise RuntimeError("cleanup failed")

    async def start() -> None:
        tasks.append(asyncio.create_task(cancelled()))
        tasks.append(asyncio.create_task(failed()))
        await asyncio.sleep(0)

    runner = asyncio.Runner()
    try:
        runner.run(start())
        failures = shutdown.cancel_and_drain(runner)
        assert sorted(cleaned) == ["cancelled", "failed"]
        assert len(failures) == 1
        assert isinstance(failures[0], RuntimeError)
        assert str(failures[0]) == "cleanup failed"
    finally:
        runner.close()


def test_hard_exit_flushes_sinks_in_order_before_exiting(unit_home: Path) -> None:
    env = dict(os.environ)
    env.update(MEIVA_HOME=str(unit_home), MEIVA_CONFIG_FETCH="skip")
    result = run_bounded(
        [sys.executable, str(_CHILD), "--hard-exit-order"],
        cwd=Path(__file__).resolve().parents[3],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=20,
    )
    assert result.returncode == 7
    assert result.stdout.splitlines() == ["loguru", "logging", "stdout", "stderr"]


def test_hard_exit_reports_a_failed_step_on_stderr_and_still_exits(unit_home: Path) -> None:
    env = dict(os.environ)
    env.update(MEIVA_HOME=str(unit_home), MEIVA_CONFIG_FETCH="skip")
    result = run_bounded(
        [sys.executable, str(_CHILD), "--hard-exit-broken"],
        cwd=Path(__file__).resolve().parents[3],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 3
    assert "closing the loguru sinks failed" in result.stderr
    assert "sink close failed" in result.stderr


def test_shutdown_line_logs_a_registered_event(loguru_records: LogRecords) -> None:
    """The stop line carries the daemon name as `service=` and derives a
    registered event."""
    from base.events import EVENTS

    previous = signal.getsignal(signal.SIGTERM)
    try:
        shutdown.install_graceful_shutdown("pg-backup")
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGTERM)
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert loguru_records.messages() == ["[pg-backup] received SIGTERM, shutting down"]
    assert loguru_records[0]["level"].name == "INFO"
    extra = loguru_records[0]["extra"]
    assert extra["service"] == "pg-backup"
    assert extra["sig"] == "SIGTERM"
    assert "label" not in extra
    assert (extra.get("event") or "log") in EVENTS


_CHILD = Path(__file__).with_name("child_graceful_stop.py")


async def test_root_graceful_stop_runs_the_product_handlers_cleanup(unit_home: Path) -> None:
    """meiva-root owns service stop: its graceful `down` (what a normal stop
    requests) must reach the product handler, whose cleanup then runs."""
    started, marker = (unit_home / name for name in ("started", "clean"))
    env = (("MEIVA_HOME", str(unit_home)), ("MEIVA_CONFIG_FETCH", "skip"))
    command = (sys.executable, str(_CHILD), str(started), str(marker))
    unit = UnitManifest("svc", command, RestartPolicy.NEVER, "root", env)
    owner = Supervisor(
        UnitRegistry([unit]), run_dir=unit_home / "root", config=SupervisorConfig(stop_timeout_s=10)
    )
    await owner.start()
    try:
        await asyncio.to_thread(
            poll_until, started.exists, timeout=20, what="product signal handler is ready"
        )
        await owner.down("svc")
        assert marker.read_text() == "clean"
    finally:
        await owner.shutdown()


def test_plugin_restart_runs_the_product_handlers_cleanup(unit_home: Path) -> None:
    repo = Path(__file__).resolve().parents[3]
    marker = unit_home / "plugin-clean"
    env = dict(os.environ)
    env.update(MEIVA_HOME=str(unit_home), MEIVA_CONFIG_FETCH="skip")
    result = subprocess.run(  # noqa: S603 — fixed test-owned Python script
        [
            sys.executable,
            str(Path(__file__).with_name("plugin_restart_probe.py")),
            str(marker),
            str(unit_home / "plugins"),
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "clean"
