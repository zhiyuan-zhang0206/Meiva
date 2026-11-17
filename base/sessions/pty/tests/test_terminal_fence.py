from __future__ import annotations

from pathlib import Path

from base.sessions.pty import terminal_fence


def test_record_persists_the_largest_cutoff(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert terminal_fence.record(home, 7, 4) == 4
    assert terminal_fence.record(home, 7, 2) == 4
    assert terminal_fence.record(home, 8, 3) == 3

    assert terminal_fence.read(home).cutoffs == {7: 4, 8: 3}


def test_unreadable_state_is_explicit(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = terminal_fence.state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json")

    assert terminal_fence.read(home).error is not None
