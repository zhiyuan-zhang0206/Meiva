# meiva:note
# Every launch wait measures against the `Timers` its launch site hands down, and against nothing
# else. These tests drive each wait with `ManualTimers` and a screen that never changes, so a
# whole window of seconds is spent on the fake clock: the sleeps are recorded on the fake timers
# and no wall time passes. A wait that reads `SYSTEM_TIMERS` for itself — the shape before the site
# carried its clock — records no fake sleep and burns the same window in wall time instead, so
# pointing any wait back at the process clock turns its test here red.
from __future__ import annotations

import time
from pathlib import Path
from typing import cast

import pytest

from base.clock.tests.manual_timers import ManualTimers
from meiva.shell.coding_tools import _claude_checks, codex
from meiva.shell.coding_tools.plans.pacing import Pacing
from meiva.shell.sessions import ShellSessions

_SESSION = 7
# meiva:note
# What a wait that spends its window on a fake clock may cost in wall time. Every window below is
# whole seconds of fake time, so anything close to that in wall time means the wait slept for real.
_WALL_BUDGET_SECONDS = 1.0


# meiva:note
# One session that shows a fixed screen: the three things a launch wait asks of a session — its
# screen, a typed line, a key — with the lines and keys kept for the test to read back.
class FixedScreen:
    def __init__(self, screen: str) -> None:
        self._screen = screen
        self.lines: list[str] = []
        self.keys: list[str] = []

    def capture(self, session_id: int, lines: int, *, scrollback: bool) -> str:
        assert session_id == _SESSION
        return self._screen

    def send(self, session_id: int, cmd: str, *, enter: bool = True) -> None:
        self.lines.append(cmd)

    def send_keys(self, session_id: int, *keys: str) -> None:
        self.keys.extend(keys)


def _screen(screen: str) -> ShellSessions:
    return cast(ShellSessions, FixedScreen(screen))


def test_the_codex_readiness_wait_spends_its_window_on_the_timers_it_is_given(
    capsys: pytest.CaptureFixture[str],
) -> None:
    timers = ManualTimers()
    started = time.monotonic()

    codex.wait_for_ready(_screen(""), _SESSION, Pacing(5.0, 0.5, 0.1), timers)

    assert timers.sleeps == [0.5] * 10
    assert time.monotonic() - started < _WALL_BUDGET_SECONDS
    assert "timeout after" in capsys.readouterr().out


def test_the_submission_check_spends_its_window_on_the_timers_it_is_given(
    capsys: pytest.CaptureFixture[str],
) -> None:
    timers = ManualTimers()
    shell = FixedScreen("")
    started = time.monotonic()

    codex.verify_submitted(cast(ShellSessions, shell), _SESSION, Pacing(5.0, 0.5, 0.25), timers)

    assert timers.sleeps == [0.5] * 10 + [0.25]
    assert shell.keys == ["Enter"]
    assert time.monotonic() - started < _WALL_BUDGET_SECONDS
    assert "still no Working signal" in capsys.readouterr().out


def test_the_status_card_wait_spends_its_window_on_the_timers_it_is_given() -> None:
    timers = ManualTimers()
    shell = FixedScreen("")
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="could not read the Codex session id"):
        codex.read_session_id(cast(ShellSessions, shell), _SESSION, Pacing(4.0, 0.5, 0.0), timers)

    assert timers.sleeps == [0.5] * 8
    assert shell.lines == ["/status"]
    assert shell.keys == ["Enter"]
    assert time.monotonic() - started < _WALL_BUDGET_SECONDS


def test_the_app_server_wait_spends_its_window_on_the_timers_it_is_given(tmp_path: Path) -> None:
    timers = ManualTimers()
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="did not become ready"):
        codex.wait_for_app_server(f"unix://{tmp_path}/absent.sock", Pacing(2.0, 0.5, 0.0), timers)

    assert timers.sleeps == [0.5] * 4
    assert time.monotonic() - started < _WALL_BUDGET_SECONDS


def test_the_claude_panel_wait_spends_its_window_on_the_timers_it_is_given() -> None:
    timers = ManualTimers()
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="Claude Code UI did not appear"):
        _claude_checks.wait_for_ready(
            _screen("$ "),
            _SESSION,
            Pacing(3.0, 0.5, 0.1),
            timers,
            failure_marker=None,
            resumed=False,
        )

    assert timers.sleeps == [0.5] * 6
    assert time.monotonic() - started < _WALL_BUDGET_SECONDS
