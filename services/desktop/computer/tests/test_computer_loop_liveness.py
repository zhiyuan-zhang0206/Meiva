from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import threading
from contextlib import suppress
from pathlib import Path
from typing import cast

import pytest

import services.desktop.computer.mcp_daemon as daemon_mod
from base.clock.tests.fakes import system_clock
from base.db import Database
from base.host.permissions_helper.client import PermissionsHelperClient
from services.desktop.computer.tests.slices import short_sock_dir

_CHILD = Path(__file__).with_name("loop_watchdog_child.py")
_REPO_ROOT = Path(__file__).resolve().parents[4]


# meiva:note
# Run the watchdog child (`blocked` or `beating`) as its own process from the repository root.
def _run_child(mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(_CHILD), mode],
        cwd=_REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_a_blocked_loop_is_dumped_and_the_process_exits_for_a_restart() -> None:
    result = _run_child("blocked")

    assert result.returncode == 1
    assert "run loop silent for over 1.0s" in result.stderr
    assert "thread stacks follow; exiting for a supervisor restart" in result.stderr


def test_a_live_loop_outlasts_the_window_and_shuts_down_cleanly() -> None:
    result = _run_child("beating")

    assert result.returncode == 0, result.stderr
    assert "run loop silent" not in result.stderr


async def test_guard_loop_liveness_beats_from_the_loop() -> None:
    watchdog = daemon_mod.guard_loop_liveness(
        asyncio.get_running_loop(), 1.0, timers=system_clock(), beat_s=0.01, check_s=5.0
    )
    try:
        await asyncio.sleep(0.3)
        assert watchdog.stale_for() < 0.3
    finally:
        watchdog.stop()
        watchdog.join(timeout=1.0)
    assert not watchdog.is_alive()


@pytest.mark.parametrize("cancel", [False, True])
async def test_run_serves_until_stopped_then_joins_its_watchdog(
    database: Database, cancel: bool
) -> None:
    d, sock, cleanup = short_sock_dir()
    serving = asyncio.create_task(
        daemon_mod.run(
            sock=str(sock),
            database=lambda: database,
            helper=PermissionsHelperClient(d / "unserved-helper.sock"),
            clock=system_clock(),
            home=d,
        )
    )
    try:
        for _ in range(250):
            if Path(sock).exists():
                break
            await asyncio.sleep(0.02)
        assert Path(sock).exists(), "the daemon never started listening"
        reader, writer = await asyncio.open_unix_connection(str(sock))
        try:
            writer.write(b'{"id": 1, "method": "ping"}\n')
            await writer.drain()
            assert json.loads(await asyncio.wait_for(reader.readline(), timeout=5.0)) == {
                "id": 1,
                "ok": True,
                "result": "pong",
            }
        finally:
            writer.close()
            await writer.wait_closed()
        assert any(t.name == "computer-mcp-loop-watchdog" for t in threading.enumerate())

        if cancel:
            serving.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(serving, timeout=10)
        else:
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.wait_for(serving, timeout=10)

        assert not Path(sock).exists()
        assert not any(
            t.name == "computer-mcp-loop-watchdog" and t.is_alive() for t in threading.enumerate()
        )
    finally:
        serving.cancel()
        try:
            with suppress(asyncio.CancelledError):
                await serving
        finally:
            cleanup(d)


class _FakeServer:
    def __init__(self) -> None:
        self.waited = False

    async def wait_closed(self) -> None:
        self.waited = True


async def test_bounded_cleanup_cancels_clients_and_waits_for_the_listener() -> None:
    hung = asyncio.Event()

    async def client() -> None:
        await hung.wait()

    task = asyncio.create_task(client())
    await asyncio.sleep(0)
    server = _FakeServer()

    await daemon_mod.bounded_cleanup(cast(asyncio.AbstractServer, server), {task}, drain_s=1.0)

    assert task.cancelled()
    assert server.waited


def test_bounded_cleanup_dumps_and_exits_when_a_client_survives_cancel() -> None:
    result = _run_child("stubborn-cleanup")

    assert result.returncode == 1
    assert re.search(r"shutdown cleanup did not finish within 0\.05s", result.stderr)
    assert "thread stacks follow; exiting for a supervisor restart" in result.stderr
