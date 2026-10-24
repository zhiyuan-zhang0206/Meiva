# meiva:note
# A captured shell tail is trimmed of blank padding before it is returned.
#
# A cursor-addressed TUI's pyte scrollback is dominated by blank rows (each full-screen redraw
# scrolls cleared rows into the history), so a plain tail can open with dozens of empty lines —
# the shell-monitor page rendered those as a huge blank region above the real output. Trailing
# blank screen rows below the last line of a short session did the mirror-image damage (the
# bottom-anchored pane scrolled the real output above the fold). Blank padding at the extremes is
# never meaningful output; interleaved blanks are preserved.

from __future__ import annotations

import pytest

from ops.cluster_status.model import trim_captured_lines


def test_leading_and_trailing_blank_padding_is_trimmed() -> None:
    captured = "\n\n\nreal line one\nreal line two\n\nreal line three\n\n\n"

    assert trim_captured_lines(captured) == [
        "real line one",
        "real line two",
        "",
        "real line three",
    ]


# meiva:note
# A session that has produced no real output yet must not render as a wall of blank lines — the
# monitor page shows its "(no output)" state.
@pytest.mark.parametrize("captured", ["", "\n\n\n", "  \n\t\n   "])
def test_output_that_is_all_blank_is_empty(captured: str) -> None:
    assert trim_captured_lines(captured) == []


def test_a_plain_log_tail_passes_through_unchanged() -> None:
    assert trim_captured_lines("line one\nline two\nline three\n") == [
        "line one",
        "line two",
        "line three",
    ]
