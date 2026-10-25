from __future__ import annotations

from pathlib import Path

import services.desktop.browser.macos_readiness as readiness
from base.clock import Timers


def _evaluate_macos(
    *,
    console_out: str = "meiva\n",
    console_code: int = 0,
    gui_code: int = 0,
    managername: str = "Aqua",
    managername_code: int = 0,
    keychain_code: int = 0,
) -> readiness.StartupReadiness:
    return readiness.evaluate_startup_readiness(
        macos=True,
        uid=501,
        account="meiva",
        console=readiness.ProbeResult(console_code, console_out, ""),
        gui=readiness.ProbeResult(gui_code, "", ""),
        domain=readiness.ProbeResult(managername_code, managername, ""),
        keychain=readiness.ProbeResult(keychain_code, "", "errSecInteractionNotAllowed"),
    )


def test_non_macos_needs_no_macos_readiness_probe() -> None:
    result = readiness.evaluate_startup_readiness(
        macos=False,
        uid=501,
        account="meiva",
        console=readiness.ProbeResult(1, "", ""),
        gui=readiness.ProbeResult(1, "", ""),
        domain=readiness.ProbeResult(1, "", ""),
        keychain=readiness.ProbeResult(1, "", ""),
    )
    assert result.ready is True


def test_macos_ready_only_after_console_gui_and_keychain_checks(tmp_path: Path) -> None:
    cmds = list(readiness.macos_probe_commands(501, tmp_path))
    assert _evaluate_macos().ready is True
    assert cmds == [
        ["/usr/bin/stat", "-f%Su", "/dev/console"],
        ["/bin/launchctl", "print", "gui/501"],
        ["/bin/launchctl", "managername"],
        [
            "/usr/bin/security",
            "show-keychain-info",
            str(tmp_path / "Library/Keychains/login.keychain-db"),
        ],
    ]


def test_macos_refuses_start_without_active_console_user() -> None:
    result = _evaluate_macos(console_out="root\n")
    assert result.ready is False
    assert result.reason is not None
    assert "GUI login session" in result.reason


def test_macos_refuses_start_when_login_keychain_is_not_ready() -> None:
    result = _evaluate_macos(keychain_code=36)
    assert result.ready is False
    assert result.reason is not None
    assert "login Keychain" in result.reason


def test_macos_refuses_start_outside_the_gui_login_session() -> None:
    pre = readiness.evaluate_console_and_gui(
        macos=True,
        uid=501,
        account="meiva",
        console=readiness.ProbeResult(0, "meiva\n", ""),
        gui=readiness.ProbeResult(0, "", ""),
        domain=readiness.ProbeResult(0, "Background", ""),
    )
    assert pre is not None
    assert pre.ready is False
    assert pre.reason is not None and "GUI login session" in pre.reason


def test_macos_domain_probe_failure_falls_through_to_keychain() -> None:
    pre = readiness.evaluate_console_and_gui(
        macos=True,
        uid=501,
        account="meiva",
        console=readiness.ProbeResult(0, "meiva\n", ""),
        gui=readiness.ProbeResult(0, "", ""),
        domain=readiness.ProbeResult(1, "", ""),
    )
    assert pre is None
    assert _evaluate_macos(managername="", managername_code=1).ready is True


class _RecordingTimers:
    def __init__(self, sleeps: list[float]) -> None:
        self._sleeps = sleeps

    def monotonic(self) -> float:
        return 0.0

    def sleep(self, seconds: float) -> None:
        self._sleeps.append(seconds)

    async def asleep(self, seconds: float) -> None:
        self._sleeps.append(seconds)


def test_wait_loop_returns_when_ready_and_records_no_state(tmp_path: Path) -> None:
    sleeps: list[float] = []
    timers: Timers = _RecordingTimers(sleeps)

    readiness.wait_for_browser_startup_readiness(
        timers=timers,
        readiness_sequence=[
            readiness.StartupReadiness(False, "no GUI login session"),
            readiness.StartupReadiness(True),
        ],
    )

    assert sleeps == [readiness.READINESS_RETRY_S]
    assert sorted(tmp_path.rglob("*")) == []


def test_degraded_wait_reason_probes_directly() -> None:
    reason = readiness.degraded_reason_from_outcome(
        readiness.StartupReadiness(ready=False, reason="login Keychain is not ready"),
        macos=True,
    )
    assert reason == "login Keychain is not ready"


def test_degraded_wait_reason_is_none_when_ready() -> None:
    reason = readiness.degraded_reason_from_outcome(
        readiness.StartupReadiness(ready=True),
        macos=True,
    )
    assert reason is None


def test_degraded_wait_reason_fails_safe_when_the_probe_raises_on_macos() -> None:
    err = OSError("probe exploded")
    assert (
        readiness.degraded_reason_from_outcome(err, macos=True)
        == "macOS startup readiness could not be probed"
    )
    assert readiness.degraded_reason_from_outcome(err, macos=False) is None
