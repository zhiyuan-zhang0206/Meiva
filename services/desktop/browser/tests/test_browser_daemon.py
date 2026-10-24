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
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import services.desktop.browser.daemon as bd
from base.config import settings
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import fake_host


def test_assert_capable_raises_reason_with_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """assert_browser_capable raises the browser_incapability reason, prefixed
    with the meiva-browser tag, so a launch on an incapable host dies loudly with
    the same wording `meiva status` shows."""

    def fake_browser_incapability(_chrome: str | None, *, host: Host) -> str | None:
        return "no display (headless server)"

    monkeypatch.setattr(bd, "browser_incapability", fake_browser_incapability)  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(RuntimeError, match=r"meiva-browser: no display"):
        bd.assert_browser_capable(host=fake_host())


def test_assert_capable_ok_when_capable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bd, "browser_incapability", lambda _chrome, **_kwargs: None)  # pyright: ignore[reportUnknownArgumentType]
    bd.assert_browser_capable(host=fake_host())  # no raise


def test_chrome_args_has_debug_port_and_profile(tmp_path: Path) -> None:
    args = bd.chrome_args("/chrome", 9222, tmp_path / "prof")
    assert args[0] == "/chrome"
    assert "--remote-debugging-port=9222" in args
    assert f"--user-data-dir={tmp_path / 'prof'}" in args
    assert "--no-first-run" in args
    assert not any(a.startswith("--headless") for a in args)


@pytest.mark.parametrize("restore", [False, True])
def test_main_asserts_capable_then_execvps_chrome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, restore: bool
) -> None:
    """main() must fail-loud-check capability before launching, then exec the
    resolved Chrome with the configured port but no automatic page."""
    order: list[str] = []

    def fake_assert_browser_capable(*, host: Host) -> None:
        order.append("capable")

    fake_chrome = tmp_path / "chrome"
    argv_out = tmp_path / "argv.json"
    fake_chrome.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(argv_out)!r}).write_text(json.dumps(sys.argv), encoding='utf-8')\n",
        encoding="utf-8",
    )
    fake_chrome.chmod(0o755)
    monkeypatch.setattr(bd, "assert_browser_capable", fake_assert_browser_capable)
    monkeypatch.setattr(
        bd,
        "cdp_reachable",
        lambda _p: (order.append("port"), False)[1],  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(bd, "resolve_chrome_binary", lambda _chrome, **_kwargs: str(fake_chrome))  # pyright: ignore[reportUnknownArgumentType]

    def _profile() -> Path:
        order.append("profile")
        return tmp_path / "prof"

    monkeypatch.setattr(bd, "profile_dir", _profile)
    monkeypatch.setattr(
        bd.macos_readiness, "wait_for_browser_startup_readiness", lambda: order.append("ready")
    )
    monkeypatch.setattr(
        bd.browser_profile,
        "validate_local_state",
        lambda _profile: None,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(bd, "logs_dir", lambda: tmp_path)
    monkeypatch.setattr(settings.services, "app_port", 3001)
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://10.0.0.72:8000")
    with pytest.raises(SystemExit) as exc:
        bd.main(restore_last_session=restore)
    assert exc.value.code == 1
    captured_args: list[str] = json.loads(argv_out.read_text(encoding="utf-8"))
    assert order == ["capable", "port", "ready", "profile"]
    assert captured_args[0] == str(fake_chrome)
    # main() takes the port from settings, and a cluster gets one out of its own
    # port block — a literal here only holds on a default-home install.
    assert f"--remote-debugging-port={settings.services.browser_cdp_port}" in captured_args
    # meiva:note
    # The flag is the ledger's decision, and nothing else's.
    assert ("--restore-last-session" in captured_args) is restore
    # Even a gateway host with an app port must leave Chrome's first page alone.
    assert len([arg for arg in captured_args if not arg.startswith("--")]) == 1


def test_main_propagates_capability_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*, host: Host) -> None:
        raise RuntimeError("no display")

    monkeypatch.setattr(bd, "assert_browser_capable", _boom)
    with pytest.raises(RuntimeError, match="no display"):
        bd.main()


def test_cdp_reachable_true_on_200(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = MagicMock(status=200)
    cm = MagicMock()
    cm.__enter__.return_value = resp
    monkeypatch.setattr(bd.urllib.request, "urlopen", lambda *_a, **_k: cm)  # pyright: ignore[reportUnknownArgumentType]
    assert bd.cdp_reachable(9222) is True


def test_cdp_reachable_false_on_connection_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refused(*_a: object, **_k: object) -> None:
        raise bd.urllib.error.URLError("connection refused")

    monkeypatch.setattr(bd.urllib.request, "urlopen", _refused)
    assert bd.cdp_reachable(9222) is False


def test_main_refuses_when_cdp_port_already_served(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A Chrome already on the CDP port (e.g. a hand-started one holding the
    profile lock) makes main() refuse with a non-zero exit and never exec a
    colliding second Chrome — so the healthcheck/watchdog don't churn a
    profile-lock crash."""

    def fake_assert_browser_capable(*, host: Host) -> None:
        return None

    monkeypatch.setattr(bd, "assert_browser_capable", fake_assert_browser_capable)
    monkeypatch.setattr(bd, "cdp_reachable", lambda _p: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(bd, "profile_dir", lambda: tmp_path / "chrome-profile")
    with pytest.raises(SystemExit) as exc:
        bd.main()
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
