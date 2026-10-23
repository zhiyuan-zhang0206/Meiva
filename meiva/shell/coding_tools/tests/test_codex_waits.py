# meiva:note
# What a Codex launch reads from the screen, and its waits against real sessions and a real socket.
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

from meiva.shell.coding_tools import codex
from meiva.shell.coding_tools.plans.codex_screen import (
    dead_session_warning,
    is_working,
    parse_session_id,
    ready_timeout_line,
    ui_state,
)
from meiva.shell.coding_tools.plans.pacing import Pacing
from meiva.shell.session_env import SessionHost
from meiva.shell.sessions import ShellSessions

_SESSION = "01a0e1ac-adc7-7d33-bd13-8ce2c6a686c5"
_STATUS_CARD = f"│  Directory:  /ws\n│  Session:                     {_SESSION}   │\n"
_QUICK = Pacing(timeout=0.5, poll=0.05, settle=0.05)
_PATIENT = Pacing(timeout=20.0, poll=0.05, settle=0.1)


def test_a_short_screen_is_not_rendered() -> None:
    assert ui_state("") == "blank"
    assert ui_state("x" * 50) == "blank"


def test_a_frame_drawn_during_mcp_startup_is_not_ready() -> None:
    # meiva:note
    # `len(output) > 50` alone is a false ready signal: the TUI renders its frame during "Starting MCP servers (0/2)", and a message sent then has its Enter swallowed.
    assert ui_state("x" * 51 + "\nStarting MCP servers (0/2)") == "starting_mcp"
    assert ui_state("x" * 51) == "ready"


def test_working_is_the_submission_signal() -> None:
    assert is_working("• Working (3s • esc to interrupt)")
    assert not is_working("composer idle")


def test_the_session_id_is_read_from_the_status_card() -> None:
    assert parse_session_id(_STATUS_CARD) == _SESSION
    assert parse_session_id("› /status") is None
    assert parse_session_id("Session: not-a-uuid") is None


def test_the_operator_is_told_what_could_not_finish() -> None:
    assert "never rendered" in ready_timeout_line(90.0, rendered=False)
    assert "rendered, still starting MCP" in ready_timeout_line(90.0, rendered=True)
    warning = dead_session_warning("Enter retry", "session 7 is not this agent's")
    assert "WARNING: submission unverified" in warning
    assert "(Enter retry failed: session 7 is not this agent's)" in warning


def test_the_app_server_wait_accepts_a_bound_socket(short_dir: Path) -> None:
    path = short_dir / "probe.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    accepted: list[bool] = []

    def accept_once() -> None:
        connection, _ = listener.accept()
        accepted.append(True)
        connection.close()

    thread = threading.Thread(target=accept_once, daemon=True)
    thread.start()
    try:
        codex.wait_for_app_server(f"unix://{path}", Pacing(timeout=5.0, poll=0.05, settle=0.0))
    finally:
        thread.join(timeout=5)
        listener.close()
    assert accepted == [True]


def test_the_app_server_wait_fails_loudly_when_absent(tmp_path: Path) -> None:
    log_path = tmp_path / "app-server.log"
    with pytest.raises(RuntimeError, match="did not become ready") as excinfo:
        codex.wait_for_app_server(
            f"unix://{tmp_path}/missing.sock",
            Pacing(timeout=0.4, poll=0.05, settle=0.0),
            log_path=log_path,
        )
    assert str(log_path) in str(excinfo.value)


def _running(shell: ShellSessions, host: SessionHost, home: Path, line: str) -> int:
    session_id, _full = shell.create("codex-probe", cwd=home, ttl=120.0, host=host)
    shell.send(session_id, line)
    return session_id


def test_the_ready_wait_passes_mcp_startup(
    live_shell: ShellSessions,
    live_host: SessionHost,
    unit_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    line = f"echo 'Starting MCP servers (0/2)'; sleep 0.5; clear; echo '{'x' * 60}'"
    session_id = _running(live_shell, live_host, unit_home, line)

    codex.wait_for_ready(live_shell, session_id, _PATIENT)

    assert "-> ready (MCP startup finished)" in capsys.readouterr().out


def test_the_ready_wait_says_so_and_goes_on_when_the_screen_never_renders(
    live_shell: ShellSessions,
    live_host: SessionHost,
    unit_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session_id = _running(live_shell, live_host, unit_home, "PS1=; clear; sleep 30")
    deadline = time.monotonic() + 20
    while live_shell.capture(session_id, 200, scrollback=False).strip():
        assert time.monotonic() < deadline, "the screen never cleared"
        time.sleep(0.05)

    codex.wait_for_ready(live_shell, session_id, _QUICK)

    assert "timeout after" in capsys.readouterr().out


def test_the_ready_wait_fails_when_the_session_has_ended(live_shell: ShellSessions) -> None:
    with pytest.raises(RuntimeError, match="ended before it was ready"):
        codex.wait_for_ready(live_shell, 999, _QUICK)


def test_the_session_id_is_read_after_asking_for_status(
    live_shell: ShellSessions, live_host: SessionHost, unit_home: Path
) -> None:
    line = f"while read l; do [ \"$l\" = /status ] && echo 'Session:   {_SESSION}'; done"
    session_id = _running(live_shell, live_host, unit_home, line)

    assert codex.read_session_id(live_shell, session_id, _PATIENT) == _SESSION


def test_a_status_that_parked_gets_one_enter(
    live_shell: ShellSessions, live_host: SessionHost, unit_home: Path
) -> None:
    # meiva:note
    # The first line the session reads is `/status`, which the TUI would leave in its composer; the card only appears once the Enter has been sent.
    line = f"read a; read b; echo 'Session:   {_SESSION}'"
    session_id = _running(live_shell, live_host, unit_home, line)

    assert codex.read_session_id(live_shell, session_id, Pacing(5.0, 0.05, 0.0)) == _SESSION


def test_no_session_id_fails_the_launch(
    live_shell: ShellSessions, live_host: SessionHost, unit_home: Path
) -> None:
    session_id = _running(live_shell, live_host, unit_home, "echo '› /status'")

    with pytest.raises(RuntimeError, match="could not read the Codex session id"):
        codex.read_session_id(live_shell, session_id, Pacing(0.4, 0.05, 0.0))


def test_submission_is_confirmed_when_working_shows(
    live_shell: ShellSessions,
    live_host: SessionHost,
    unit_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session_id = _running(live_shell, live_host, unit_home, 'sleep 0.3; echo Wor"king"; sleep 30')

    codex.verify_submitted(live_shell, session_id, _PATIENT)

    assert "-> submitted (Working visible)" in capsys.readouterr().out


def test_a_parked_message_gets_one_enter_and_then_works(
    live_shell: ShellSessions,
    live_host: SessionHost,
    unit_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session_id = _running(live_shell, live_host, unit_home, 'read a; echo Wor"king"; sleep 30')

    codex.verify_submitted(live_shell, session_id, _QUICK)

    out = capsys.readouterr().out
    assert "not submitted within window; sending Enter once" in out
    assert "-> submitted after Enter retry" in out


def test_a_message_that_stays_parked_is_reported_not_raised(
    live_shell: ShellSessions,
    live_host: SessionHost,
    unit_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session_id = _running(live_shell, live_host, unit_home, "cat")

    codex.verify_submitted(live_shell, session_id, _QUICK)

    assert "still no Working signal" in capsys.readouterr().out


def test_submission_check_survives_a_dead_session_at_capture(
    live_shell: ShellSessions, capsys: pytest.CaptureFixture[str]
) -> None:
    codex.verify_submitted(live_shell, 999, _PATIENT)

    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "capture failed" in out


def test_submission_check_survives_a_dead_session_at_enter(
    live_shell: ShellSessions, capsys: pytest.CaptureFixture[str]
) -> None:
    # meiva:note
    # A window of zero seconds goes straight to the Enter retry, which finds the session gone.
    codex.verify_submitted(live_shell, 999, Pacing(timeout=0.0, poll=0.05, settle=0.05))

    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "Enter retry failed" in out


def test_submission_check_survives_a_dead_session_after_the_enter_retry(
    live_shell: ShellSessions,
    live_host: SessionHost,
    unit_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # meiva:note
    # The session ends the moment it reads the Enter the check sends, so the capture that follows finds it gone.
    session_id = _running(live_shell, live_host, unit_home, "read a; exit 0")

    codex.verify_submitted(live_shell, session_id, Pacing(timeout=0.3, poll=0.05, settle=0.5))

    assert "capture failed" in capsys.readouterr().out
