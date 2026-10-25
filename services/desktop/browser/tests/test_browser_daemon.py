"""services.desktop.browser.daemon — capability assertion, arg construction, and the
exec entrypoint. The three-prong capability check (display + Chrome + npx) now
lives in base.host.system.probes.browser_incapability (tested per-prong in
base/host/system/tests/test_probes.py); assert_browser_capable is a thin raising
wrapper over it, so these tests patch browser_incapability as bound in the daemon
module. The real exec is not unit-tested (it replaces the process); the testable
surface is the pure helpers, launch ordering, and the POSIX exec path.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

import services.desktop.browser.daemon as bd
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import DARWIN_ARM64, fake_host
from services.desktop.browser import session_restore
from services.desktop.browser.macos_readiness import StartupReadiness


@contextmanager
def _serve_200_once() -> Generator[int]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def _handle() -> None:
            try:
                conn, _addr = server.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.recv(4096)
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
                    )
                except OSError:
                    return

        thread = threading.Thread(target=_handle, daemon=True)
        thread.start()
        try:
            yield port
        finally:
            thread.join(timeout=2.0)


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _capable_host(_chrome_path: Path) -> Host:
    return fake_host(
        platform=DARWIN_ARM64,
        programs={"npx": "/usr/local/bin/npx"},
    )


def test_assert_capable_raises_reason_with_tag() -> None:
    """assert_browser_capable raises the browser_incapability reason, prefixed
    with the meiva-browser tag, so a launch on an incapable host dies loudly with
    the same wording `meiva status` shows."""
    incapable = fake_host(platform=DARWIN_ARM64, programs={})
    with pytest.raises(RuntimeError, match=r"meiva-browser: no npx"):
        bd.assert_browser_capable(host=incapable, chrome_binary="/chrome")


def test_assert_capable_ok_when_capable(tmp_path: Path) -> None:
    chrome = tmp_path / "chrome"
    bd.assert_browser_capable(host=_capable_host(chrome), chrome_binary=str(chrome))


def test_chrome_args_has_debug_port_and_profile(tmp_path: Path) -> None:
    args = bd.chrome_args("/chrome", 9222, tmp_path / "prof")
    assert args[0] == "/chrome"
    assert "--remote-debugging-port=9222" in args
    assert f"--user-data-dir={tmp_path / 'prof'}" in args
    assert "--no-first-run" in args
    assert not any(a.startswith("--headless") for a in args)


# meiva:note
# `run_browser_daemon()` checks capability before launching, then spawns the
# resolved Chrome with the configured port and no automatic page.
@pytest.mark.parametrize("restore", [False, True])
def test_main_asserts_capable_then_execvps_chrome(tmp_path: Path, restore: bool) -> None:
    order: list[str] = []
    fake_chrome = tmp_path / "chrome"
    argv_out = tmp_path / "argv.json"
    fake_chrome.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(argv_out)!r}).write_text(json.dumps(sys.argv), encoding='utf-8')\n",
        encoding="utf-8",
    )
    fake_chrome.chmod(0o755)
    if restore:
        session_restore.write(
            tmp_path,
            session_restore.Ledger(
                tabs=(
                    session_restore.Tab(
                        url="https://a.example/",
                        title="",
                        pinned=False,
                        group=None,
                        index=0,
                        window=0,
                    ),
                ),
                captured_at="2026-10-22T00:00:00+00:00",
                layout=False,
                pending=session_restore.PENDING_RESTORE,
                reapply=None,
            ),
        )
    port = _closed_port()
    with pytest.raises(SystemExit) as exc:
        bd.run_browser_daemon(
            host=_capable_host(fake_chrome),
            cdp_port=port,
            chrome_binary=str(fake_chrome),
            home=tmp_path,
            profile=tmp_path / "prof",
            logs=tmp_path,
            readiness_sequence=[StartupReadiness(True)],
            poll_interval_s=0.01,
            steps_out=order,
        )
    assert exc.value.code == 1
    captured_args: list[str] = json.loads(argv_out.read_text(encoding="utf-8"))
    assert order == ["capable", "port", "ready", "profile"]
    assert captured_args[0] == str(fake_chrome)
    assert f"--remote-debugging-port={port}" in captured_args
    # meiva:note
    # The flag is the ledger's decision, and nothing else's.
    assert ("--restore-last-session" in captured_args) is restore
    # meiva:note
    # Even a gateway host with an app port must leave Chrome's first page alone.
    assert len([arg for arg in captured_args if not arg.startswith("--")]) == 1


def test_main_propagates_capability_failure(tmp_path: Path) -> None:
    incapable = fake_host(platform=DARWIN_ARM64, programs={})
    with pytest.raises(RuntimeError, match="no npx"):
        bd.run_browser_daemon(
            host=incapable,
            cdp_port=9222,
            chrome_binary="/chrome",
            home=tmp_path,
            profile=tmp_path / "prof",
            logs=tmp_path,
        )


def test_cdp_reachable_true_on_200() -> None:
    with _serve_200_once() as port:
        assert bd.cdp_reachable(port) is True


def test_cdp_reachable_false_on_connection_refused() -> None:
    assert bd.cdp_reachable(_closed_port()) is False


# meiva:note
# A Chrome already on the CDP port makes `run_browser_daemon()` refuse with a
# non-zero exit and never spawn a colliding second Chrome.
def test_main_refuses_when_cdp_port_already_served(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    chrome = tmp_path / "chrome"
    with _serve_200_once() as port, pytest.raises(SystemExit) as exc:
        bd.run_browser_daemon(
            host=_capable_host(chrome),
            cdp_port=port,
            chrome_binary=str(chrome),
            home=tmp_path,
            profile=tmp_path / "chrome-profile",
            logs=tmp_path,
        )
    assert exc.value.code == 1
    assert "already served" in capsys.readouterr().err


# meiva:note
# plan_port_occupancy decides between launching directly, adopting our cluster's healthy
# Chrome, reaping our wedged Chrome before relaunching, or refusing a foreign Chrome.
def test_plan_port_occupancy() -> None:
    assert bd.plan_port_occupancy(reachable=False, holder_pid=None, cdp_healthy=False) == "launch"
    assert bd.plan_port_occupancy(reachable=True, holder_pid=4242, cdp_healthy=True) == "adopt"
    assert (
        bd.plan_port_occupancy(reachable=True, holder_pid=4242, cdp_healthy=False)
        == "reap_and_launch"
    )
    assert bd.plan_port_occupancy(reachable=True, holder_pid=None, cdp_healthy=True) == "refuse"


# meiva:note
# watch_step returns 0 on stop, None while either the spawned handle or a cluster Chrome PID
# is alive, and a non-zero exit code once both are gone.
def test_watch_step() -> None:
    assert (
        bd.watch_step(stopping=True, proc_running=True, proc_returncode=None, cluster_pids=[4242])
        == 0
    )
    assert (
        bd.watch_step(stopping=False, proc_running=True, proc_returncode=None, cluster_pids=[])
        is None
    )
    assert (
        bd.watch_step(stopping=False, proc_running=False, proc_returncode=0, cluster_pids=[4242])
        is None
    )
    assert (
        bd.watch_step(stopping=False, proc_running=False, proc_returncode=0, cluster_pids=[]) == 1
    )
    assert (
        bd.watch_step(stopping=False, proc_running=False, proc_returncode=2, cluster_pids=[]) == 2
    )


# meiva:note
# SIGTERM to the keeper unit must exit watch_chrome cleanly (no SystemExit) and leave the
# detached child process running in its own session.
def test_watch_chrome_exits_zero_on_sigterm_without_killing_browser(tmp_path: Path) -> None:
    child_script = tmp_path / "keep_alive.py"
    child_script.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, time\n"
        "time.sleep(0.05)\n"
        "os.kill(os.getppid(), signal.SIGTERM)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    child_script.chmod(0o755)
    proc = bd.spawn_chrome(str(child_script), [str(child_script)])
    try:
        bd.watch_chrome(tmp_path, proc, poll_interval_s=0.01)
        assert proc.poll() is None
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# meiva:note
# When the child exits on its own and no cluster Chrome remains on the profile, watch_chrome
# exits non-zero so meiva-root respawns the browser unit.
def test_watch_chrome_exits_nonzero_when_browser_disappears(tmp_path: Path) -> None:
    child_script = tmp_path / "exit_zero.py"
    child_script.write_text(
        f"#!{sys.executable}\nraise SystemExit(0)\n",
        encoding="utf-8",
    )
    child_script.chmod(0o755)
    proc = bd.spawn_chrome(str(child_script), [str(child_script)])
    with pytest.raises(SystemExit) as exc:
        bd.watch_chrome(tmp_path, proc, poll_interval_s=0.01)
    assert exc.value.code == 1


def test_chrome_args_restores_the_last_session_only_on_request(tmp_path: Path) -> None:
    plain = bd.chrome_args("/chrome", 9222, tmp_path / "prof")
    assert "--restore-last-session" not in plain
    restoring = bd.chrome_args("/chrome", 9222, tmp_path / "prof", restore_last_session=True)
    assert restoring[-1] == "--restore-last-session"
