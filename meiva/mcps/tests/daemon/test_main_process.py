from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from meiva.mcps._guards import socket_is_live
from meiva.mcps._remote import RemoteMCPClient
from meiva.mcps.tests.daemon.daemon_support import fixture_entry, write_servers
from meiva.mcps.tests.line_streams import leftover_socket
from meiva.mcps.tests.line_streams import short_dir as short_dir

# meiva:note
# The daemon as it runs in production: `python -m meiva.mcps._daemon` in a process of its own, with a home of its own. It runs from a scratch working directory, so the stale-daemon reaper (which matches the project root or the home) never touches another test's daemon.


def daemon_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "meiva.mcps._daemon", *args]


def daemon_env(home: Path) -> dict[str, str]:
    return {**os.environ, "MEIVA_HOME": str(home)}


# meiva:note
# Start the daemon process; its stderr goes to `<cwd>/daemon.err`. It is killed if the test leaves it running.
@contextmanager
def running_daemon(home: Path, cwd: Path, *args: str) -> Generator[subprocess.Popen[bytes]]:
    with (cwd / "daemon.err").open("wb") as err:
        proc = subprocess.Popen(  # noqa: S603
            daemon_command(*args), env=daemon_env(home), cwd=cwd, stderr=err
        )
        try:
            yield proc
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


def wait_until_live(socket_path: str, proc: subprocess.Popen[bytes], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert proc.poll() is None, "the daemon exited during startup"
        if socket_is_live(socket_path, timeout_seconds=0.5):
            return
        time.sleep(0.1)
    raise AssertionError(f"nothing answered a ping on {socket_path}")


# meiva:note
# No argument binds the per-machine shared socket under the home; SIGTERM is the graceful exit: the
# server closes, the shared children are released and the socket file is removed.
def test_the_daemon_serves_the_shared_socket_and_stops_on_sigterm(short_dir: Path) -> None:
    home = short_dir / "h"
    home.mkdir()
    write_servers(home, {"fs": fixture_entry()})
    socket_path = str(home / "run" / "mcp_daemon.sock")

    with running_daemon(home, short_dir) as proc:
        wait_until_live(socket_path, proc)
        tools = RemoteMCPClient(socket_path).list_tools("fs", timeout_seconds=30.0, agent_id=None)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=15) == 0

    assert [tool["name"] for tool in tools][:2] == ["echo", "pid"]
    assert not Path(socket_path).exists()


def test_a_launch_with_two_arguments_fails_fast(short_dir: Path) -> None:
    with running_daemon(short_dir / "h", short_dir, "/srv/a.sock", "/srv/b.sock") as proc:
        assert proc.wait(timeout=30) == 1
    assert (
        "Usage: python -m meiva.mcps._daemon [socket_path]"
        in (short_dir / "daemon.err").read_text()
    )


# meiva:note
# A leftover (dead) socket file does not block startup: only a LIVE one does.
def test_a_launch_over_a_dead_socket_file_takes_it_over(short_dir: Path) -> None:
    path = leftover_socket(short_dir / "stale.sock")
    with running_daemon(short_dir / "h", short_dir, str(path)) as proc:
        wait_until_live(str(path), proc)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=15) == 0


# meiva:note
# A stray launch must not steal the socket from the serving daemon (Task #1142).
async def test_a_launch_over_a_live_socket_refuses_and_leaves_it_alone(short_dir: Path) -> None:
    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while await reader.readline():
            writer.write(b'{"id": 0, "ok": true, "result": "pong"}\n')
            await writer.drain()
        writer.close()

    path = short_dir / "live.sock"
    server = await asyncio.start_unix_server(answer, path=str(path))
    try:
        inode = path.stat().st_ino
        process = await asyncio.create_subprocess_exec(
            *daemon_command(str(path)),
            env=daemon_env(short_dir / "h"),
            cwd=short_dir,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), 60)
        assert process.returncode == 1
        assert f"already serving {path}" in stderr.decode()
        assert path.stat().st_ino == inode
        assert await asyncio.to_thread(socket_is_live, str(path), timeout_seconds=2.0) is True
    finally:
        server.close()
        await server.wait_closed()
