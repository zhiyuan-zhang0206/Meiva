from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import psutil
import pytest

from base.clock.tests.fakes import system_clock
from base.paths import chrome_mcp_socket
from services.desktop.browser.tests.test_browser_mcp_daemon_tab_groups import WedgedChrome


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return Path(tempfile.mkdtemp(prefix="meiva-mcp-home-", dir="/tmp"))


_FAKE_UPSTREAM = r"""
import json, os, signal, sys

with open(os.path.join(os.path.dirname(sys.argv[0]), "upstream-births"), "a") as births:
    births.write("birth\n")

def _die(*_args: object) -> None:
    os._exit(0)

signal.signal(signal.SIGTERM, _die)
signal.signal(signal.SIGINT, _die)

mode = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("FAKE_UPSTREAM_MODE", "ok")
if mode == "hang-initialize":
    while True:  # never answer; exit only on TERM/EOF
        if not sys.stdin.readline():
            os._exit(0)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method = msg.get("method")
    if method == "initialize":
        proto = (msg.get("params") or {}).get("protocolVersion", "2024-11-05")
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0", "id": msg["id"],
            "result": {"protocolVersion": proto, "capabilities": {},
                       "serverInfo": {"name": "fake", "version": "0"}},
        }) + "\n")
        sys.stdout.flush()
        if mode == "die-after-init":
            os._exit(0)
    elif method in ("ping", "tools/list", "tools/call"):
        if method == "tools/list":
            result = {"tools": []}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "ok"}], "isError": False}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
        sys.stdout.flush()
        if mode == "die-after-init" and method == "initialize":
            os._exit(0)  # answer the handshake, then die like a crashed upstream
os._exit(0)
"""


class _ChromeFake:
    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self.accepted = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.accepted += 1
            conn.close()

    def close(self) -> None:
        self._stop.set()
        with suppress(OSError):
            self._sock.close()


def _fake_bin(tmp_path: Path, mode: str) -> tuple[Path, Path]:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    upstream = tmp_path / "fake_upstream.py"
    upstream.write_text(_FAKE_UPSTREAM, encoding="utf-8")
    npx = bin_dir / "npx"
    npx.write_text(f'#!/bin/sh\nexec {sys.executable} {upstream} {mode} "$@"\n', encoding="utf-8")
    npx.chmod(npx.stat().st_mode | stat.S_IXUSR)
    return bin_dir, upstream


def _daemon_env(home: Path, bin_dir: Path, chrome: _ChromeFake, mode: str) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(
        f"MEIVA_BROWSER_CDP_PORT={chrome.port}\nMEIVA_MCP_CONNECT_TIMEOUT_SECONDS=120\n"
        "MEIVA_MACHINE_NAME=browser-mcp-test\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.update(
        {
            "MEIVA_HOME": str(home),
            "FAKE_UPSTREAM_MODE": mode,
            "PATH": f"{bin_dir}:{env['PATH']}",
        }
    )
    for key in (
        "MEIVA_CONFIG_FETCH",
        "MEIVA_GATEWAY_URL",
        "MEIVA_CLUSTER",
    ):
        env.pop(key, None)
    return env


def _socket_path(home: Path, chrome: _ChromeFake) -> Path:
    return home / "run" / f"chrome-mcp.{chrome.port}.sock"


def _spawn(
    home: Path, tmp_path: Path, bin_dir: Path, chrome: _ChromeFake, mode: str
) -> tuple[subprocess.Popen[bytes], Path]:
    log_path = tmp_path / "daemon.log"
    env = _daemon_env(home, bin_dir, chrome, mode)
    (tmp_path / "daemon.env").write_text(
        "\n".join(f"{k}={v}" for k, v in sorted(env.items())), encoding="utf-8"
    )
    log = log_path.open("wb")
    return (
        subprocess.Popen(
            [sys.executable, "-u", "-m", "services.desktop.browser.mcp_daemon"],
            env=env,
            cwd=Path(__file__).resolve().parents[4],
            stdout=log,
            stderr=subprocess.STDOUT,
        ),
        log_path,
    )


def _wait_socket(
    path: Path, daemon: subprocess.Popen[bytes], log_path: Path, timeout: float = 20.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if daemon.poll() is not None:
            log = log_path.read_text(encoding="utf-8", errors="replace")
            raise AssertionError(f"daemon exited early with rc={daemon.returncode}:\n{log[-3000:]}")
        time.sleep(0.05)
    raise AssertionError(f"daemon socket {path} did not appear within {timeout}s")


def _client_roundtrip(path: Path, request: dict[str, object]) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10)
        client.connect(str(path))
        client.sendall((json.dumps(request) + "\n").encode())
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        return json.loads(b"".join(chunks).split(b"\n", 1)[0].decode())


def _client_roundtrip_until_ok(
    path: Path, request: dict[str, object], timeout: float = 30.0
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = _client_roundtrip(path, request)
        if last.get("ok") is True:
            return last
        time.sleep(0.2)
    raise AssertionError(f"round-trip never succeeded: {last}")


def _upstream_child_pid(daemon: subprocess.Popen[bytes]) -> int:
    try:
        children = psutil.Process(daemon.pid).children(recursive=False)
    except psutil.NoSuchProcess:
        return 0
    return children[0].pid if children else 0


def _term_and_wait(daemon: subprocess.Popen[bytes], log_path: Path, timeout: float = 25.0) -> int:
    with suppress(ProcessLookupError):
        os.kill(daemon.pid, signal.SIGTERM)
    try:
        return daemon.wait(timeout=timeout)
    except subprocess.TimeoutExpired as err:
        daemon.kill()
        daemon.wait(timeout=5)
        raise AssertionError(
            f"daemon did not exit within {timeout}s after SIGTERM; log:\n"
            f"{log_path.read_text(encoding='utf-8', errors='replace')[-3000:]}"
        ) from err


@pytest.mark.flaky
def test_sigterm_normal_connection_closes_everything_except_chrome(
    home: Path, tmp_path: Path
) -> None:
    chrome = _ChromeFake()
    bin_dir, _ = _fake_bin(tmp_path, "ok")
    daemon, daemon_log = _spawn(home, tmp_path, bin_dir, chrome, "ok")
    sock_path = _socket_path(home, chrome)
    try:
        _wait_socket(sock_path, daemon, daemon_log)
        resp = _client_roundtrip_until_ok(sock_path, {"id": 1, "method": "list_tools"})
        tools = cast("list[dict[str, Any]]", resp["result"])
        assert [t["name"] for t in tools] == ["renew_page"]

        child_pid = _upstream_child_pid(daemon)
        assert child_pid, "no upstream child — the fake npx did not spawn"

        rc = _term_and_wait(daemon, daemon_log)
        assert rc == 0
        assert not sock_path.exists(), "daemon socket must be unlinked on stop"
        assert not psutil.pid_exists(child_pid), "upstream child must exit with the daemon"
        assert chrome.accepted == 0, "stop must never touch the retained Chrome"
    finally:
        if daemon.poll() is None:
            daemon.kill()
            daemon.wait(timeout=5)
        chrome.close()


@pytest.mark.flaky
def test_sigterm_during_hung_connect_interrupts_immediately(home: Path, tmp_path: Path) -> None:
    chrome = _ChromeFake()
    bin_dir, _ = _fake_bin(tmp_path, "hang-initialize")
    daemon, daemon_log = _spawn(home, tmp_path, bin_dir, chrome, "hang-initialize")
    sock_path = _socket_path(home, chrome)
    try:
        _wait_socket(sock_path, daemon, daemon_log)
        time.sleep(0.5)
        start = time.monotonic()
        rc = _term_and_wait(daemon, daemon_log)
        assert rc == 0
        assert time.monotonic() - start < 15, "TERM must interrupt a hung connect"
        assert not sock_path.exists()
    finally:
        if daemon.poll() is None:
            daemon.kill()
            daemon.wait(timeout=5)
        chrome.close()


@pytest.mark.flaky
def test_upstream_death_exits_without_reconnecting(home: Path, tmp_path: Path) -> None:
    chrome = _ChromeFake()
    bin_dir, _ = _fake_bin(tmp_path, "die-after-init")
    daemon, daemon_log = _spawn(home, tmp_path, bin_dir, chrome, "die-after-init")
    try:
        rc = daemon.wait(timeout=20)
        assert rc != 0, daemon_log.read_text(errors="replace")[-3000:]
        assert (tmp_path / "upstream-births").read_text().splitlines() == ["birth"]
        assert not _socket_path(home, chrome).exists()
    finally:
        if daemon.poll() is None:
            daemon.kill()
            daemon.wait(timeout=5)
        chrome.close()


async def _run_with_fakes(
    home: Path,
    stop: asyncio.Event,
    *,
    wedged_stack: bool = False,
    browser_cdp_port: int = 9222,
) -> tuple[asyncio.Task[None], Any]:
    from contextlib import AsyncExitStack

    from mcp import ClientSession, types

    from services.desktop.browser.mcp_daemon import serve_with_upstream

    class FakeSession:
        async def send_ping(self) -> None:
            await asyncio.sleep(0)

        async def list_tools(self) -> Any:
            return type("R", (), {"tools": []})()

        async def call_tool(self, *_a: object, **_k: object) -> Any:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="1: https://a.example/ [selected]")],
                is_error=False,
            )

    class TrackedStack:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True
            if wedged_stack:
                await asyncio.Event().wait()

    stack = TrackedStack()
    task = asyncio.create_task(
        serve_with_upstream(
            home=home,
            browser_cdp_port=browser_cdp_port,
            stop=stop,
            upstream=cast(ClientSession, FakeSession()),
            current_stack=cast(AsyncExitStack, stack),
            clock=system_clock(),
        )
    )
    await asyncio.sleep(0.3)
    return task, stack


@pytest.mark.flaky
def test_cancelled_session_task_never_skips_cleanup(home: Path, tmp_path: Path) -> None:
    import services.desktop.browser.mcp_daemon as daemon_mod

    async def scenario() -> None:
        stop = asyncio.Event()
        task, stack = await _run_with_fakes(home, stop)
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, timeout=20)
        assert stack.closed, "upstream stack must be closed after the task ended"
        assert not chrome_mcp_socket(home, 9222).exists(), "socket must be unlinked"

    asyncio.run(scenario())
    assert daemon_mod


@pytest.mark.flaky
def test_wedged_cleanup_await_is_bounded(home: Path, tmp_path: Path) -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        task, _stack = await _run_with_fakes(home, stop, wedged_stack=True)
        await asyncio.sleep(0.2)
        start = time.monotonic()
        stop.set()
        await asyncio.wait_for(task, timeout=30)
        assert time.monotonic() - start < 20, "wedged stack close must be bounded"
        assert not chrome_mcp_socket(home, 9222).exists()

    asyncio.run(scenario())


@pytest.mark.flaky
def test_stopping_the_daemon_drops_in_flight_tab_groupings(
    home: Path,
) -> None:
    async def scenario() -> None:
        async with WedgedChrome() as chrome:
            stop = asyncio.Event()
            task, _stack = await _run_with_fakes(home, stop, browser_cdp_port=chrome.port)
            reader, writer = await asyncio.open_unix_connection(
                str(chrome_mcp_socket(home, chrome.port))
            )
            request = {
                "id": 1,
                "method": "call_tool",
                "tool": "new_page",
                "args": {"url": "https://a.example/"},
                "agent_id": 7,
            }
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=5)
            in_flight = [t for t in asyncio.all_tasks() if t.get_name() == "tab-group-agent-7"]
            assert len(in_flight) == 1 and not in_flight[0].done()
            stop.set()
            await asyncio.wait_for(task, timeout=20)
            assert in_flight[0].cancelled()
            writer.close()

    asyncio.run(scenario())
