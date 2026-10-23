# meiva:note
# Claude Code launch checks: wait for the panel, then deliver the first message.
from __future__ import annotations

import time
from pathlib import Path

from meiva.shell.coding_tools._common import SessionGone, capture_screen
from meiva.shell.coding_tools.plans.claude_screen import (
    CLAUDE_NOT_FOUND,
    Verdict,
    failure_text,
    login_marker,
    paste_payload,
    screen_verdict,
)
from meiva.shell.coding_tools.plans.pacing import Pacing
from meiva.shell.sessions import ShellSessions


# meiva:note
# Send a bootstrap message through the composer's paste path (#4364).
def send_bootstrap(shell: ShellSessions, sid: int, message: str) -> None:
    shell.send(sid, paste_payload(message))


# meiva:note
# One look at the session: its visible screen (None once it has ended) and the markers the launch command leaves, read together and judged by `screen_verdict`.
def _look(shell: ShellSessions, sid: int, failure_marker: Path | None, *, resumed: bool) -> Verdict:
    screen = capture_screen(shell, sid)
    return screen_verdict(
        None if isinstance(screen, SessionGone) else screen,
        resumed=resumed,
        missing_marker=None if failure_marker is None else failure_marker.is_file(),
        login_marker=None if failure_marker is None else login_marker(failure_marker).is_file(),
    )


def _raise_if_failed(verdict: Verdict, sid: int, *, settled: bool) -> None:
    if verdict in ("missing", "logged_out", "exited"):
        if verdict == "missing":
            print(f"error: {CLAUDE_NOT_FOUND}")
        raise RuntimeError(failure_text(verdict, sid, settled=settled))


# meiva:note
# Require Claude's rendered UI before delivering any text to the session. Claude Code startup takes 5-10 seconds, so a fixed sleep often sent the contract message before the TUI was ready and lost it; a long shell prompt is not a ready Claude panel. The panel must look ready twice, `pacing.settle` apart.
def wait_for_ready(
    shell: ShellSessions,
    sid: int,
    pacing: Pacing,
    *,
    failure_marker: Path | None,
    resumed: bool,
) -> None:
    print(f"waiting for session {sid} to be ready (polling capture)...")
    deadline = time.monotonic() + pacing.timeout
    while time.monotonic() < deadline:
        verdict = _look(shell, sid, failure_marker, resumed=resumed)
        _raise_if_failed(verdict, sid, settled=False)
        if verdict == "ready":
            time.sleep(pacing.settle)
            verdict = _look(shell, sid, failure_marker, resumed=resumed)
            _raise_if_failed(verdict, sid, settled=True)
            if verdict == "ready":
                print("  -> ready (Claude Code UI)")
                return
        time.sleep(pacing.poll)
    raise RuntimeError(
        f"Claude Code UI did not appear in session {sid} within {pacing.timeout:.0f} s; "
        "no text was delivered"
    )
