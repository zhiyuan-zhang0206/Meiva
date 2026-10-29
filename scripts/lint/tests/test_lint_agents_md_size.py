from pathlib import Path

import pytest

from scripts.lint import agents_md_size as lint


def _root_with(tmp_path: Path, lines: int) -> Path:
    (tmp_path / "AGENTS.md").write_text("x\n" * lines, encoding="utf-8")
    return tmp_path


# meiva:note
# One hard line and nothing below it: a node short of the ceiling says nothing, because a "room
# left" readout is what invites filling the last lines instead of moving a section out.
def test_a_file_below_the_ceiling_is_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lint.main(repo_root=_root_with(tmp_path, 200)) == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_the_module_keeps_no_band_threshold() -> None:
    assert not hasattr(lint, "_TRANSITIONAL_FLOOR")


def test_the_ceiling_itself_passes_silently(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lint.main(repo_root=_root_with(tmp_path, 245)) == 0
    assert capsys.readouterr().out == ""


# meiva:note
# Past the line the message carries the fact, why the file is that size-limited, the repair and the
# document that owns the rule. Asserted on fragments, not sentences.
def test_one_line_past_the_ceiling_fails_with_the_reason_and_the_repair(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lint.main(repo_root=_root_with(tmp_path, 246)) == 1
    out = capsys.readouterr().out.lower()

    assert "246 lines" in out
    assert "first file every agent reads" in out
    assert "docs/conventions/" in out
    assert "agents-md-size.meiva.okf.md" in out


def test_a_missing_agents_md_fails_loud(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert lint.main(repo_root=tmp_path) == 1
    assert "not found" in capsys.readouterr().err
