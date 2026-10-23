"""`capture_shell` trims blank padding from the captured tail.

A cursor-addressed TUI's pyte scrollback is dominated by blank rows (each
full-screen redraw scrolls cleared rows into the history), so a plain tail can
open with dozens of empty lines — the shell-monitor page rendered those as a
huge blank region above the real output. Trailing blank screen rows below the
last line of a short session did the mirror-image damage (the bottom-anchored
pane scrolled the real output above the fold). Blank padding at the extremes
is never meaningful output; interleaved blanks are preserved.
"""

from __future__ import annotations

from ops.cluster_status import capture_shell
from ops.rpc_schemas import ShellInfo


class _FakeBackend:
    def __init__(self, captured: str) -> None:
        self._captured = captured
        self.calls: list[tuple[str, int]] = []

    def capture_pane(self, name: str, lines: int = 200, *, scrollback: bool = True) -> str:
        self.calls.append((name, lines))
        return self._captured


def test_capture_trims_leading_and_trailing_blank_padding() -> None:
    backend = _FakeBackend("\n\n\nreal line one\nreal line two\n\nreal line three\n\n\n")

    name, lines, created_at, uptime = capture_shell(
        42,
        7,
        lines=200,
        shell_sessions_fn=lambda _agent_id: [ShellInfo(id=7)],
        shell_backend=backend,
    )

    assert created_at is None
    assert uptime == 0

    assert lines == ["real line one", "real line two", "", "real line three"]
    assert backend.calls == [(name, 200)]
    assert name.endswith("-agent-42-shell-7")


def test_capture_of_all_blank_output_is_empty() -> None:
    """A session that has produced no real output yet must not render as a
    wall of blank lines — the monitor page shows its "(no output)" state."""
    backend = _FakeBackend("\n\n\n")

    _, lines, _, _ = capture_shell(
        42,
        3,
        lines=200,
        shell_sessions_fn=lambda _agent_id: [ShellInfo(id=3)],
        shell_backend=backend,
    )

    assert lines == []


def test_capture_without_blank_padding_is_unchanged() -> None:
    """A plain log tail (the normal case) passes through byte-for-byte."""
    backend = _FakeBackend("line one\nline two\nline three\n")

    _, lines, _, _ = capture_shell(
        42,
        9,
        lines=200,
        shell_sessions_fn=lambda _agent_id: [ShellInfo(id=9)],
        shell_backend=backend,
    )

    assert lines == ["line one", "line two", "line three"]
