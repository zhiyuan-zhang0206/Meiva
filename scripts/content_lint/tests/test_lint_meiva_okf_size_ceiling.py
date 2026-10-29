from __future__ import annotations

from pathlib import Path

import pytest

from scripts.content_lint import lint_meiva_okf as _lint

# meiva:note
# The size ceiling is one hard line with no headroom band: a node one character short of it passes
# silently, because a "room left" readout is what invites an author to fill the last characters
# instead of splitting the topic the node has outgrown. Past the line the message carries the
# reason and the remedy, not only the numbers.
MAX_CHARS: int = _lint.MAX_CHARS
MAX_LINES: int = _lint.MAX_LINES

# meiva:note
# The phrases the message must carry for an author to act on the intent rather than the number
# alone. Fragments, not sentences: the wording stays free to change, the four parts may not quietly
# drop out of it.
_INTENT_PHRASES = ("one semantic topic", "not a budget to fill")
_REMEDY_PHRASES = ("you own the semantic split", "do not trim or compress", "doc-maintenance.md")

_FRONTMATTER = """---
type: doc
title: Sized Node
description: A node built to an exact character count.
---

# Sized Node

"""


# meiva:note
# A constructed node goes under a `docs/` layer, so the placement rule (E014) sees a layered node
# and stays out of the way; these tests exercise the size ceiling, and no production symbol is
# patched to silence another rule.
def _node_path(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    return docs / "sized.meiva.okf.md"


# meiva:note
# An otherwise-valid node whose decoded length is exactly `char_count`, padded with prose-shaped
# filler on one long line so the line ceiling cannot fire on the way to the character ceiling.
def _node_of_size(tmp_path: Path, char_count: int) -> None:
    filler_len = char_count - len(_FRONTMATTER) - 1
    assert filler_len > 0, "char_count too small to hold the frontmatter"
    body = (" ".join(["word"] * (filler_len // 5 + 1)))[:filler_len]
    path = _node_path(tmp_path)
    path.write_text(_FRONTMATTER + body + "\n", encoding="utf-8")
    assert len(path.read_text(encoding="utf-8")) == char_count


# meiva:note
# An otherwise-valid node whose decoded line count is exactly `line_count`. Each filler line is
# short, so the character ceiling stays clear while the line ceiling is measured.
def _node_of_line_count(tmp_path: Path, line_count: int) -> None:
    head = _FRONTMATTER.splitlines()
    assert line_count > len(head), "line_count too small to hold the frontmatter"
    filler = ["filler line"] * (line_count - len(head))
    path = _node_path(tmp_path)
    path.write_text("\n".join(head + filler) + "\n", encoding="utf-8")
    assert len(path.read_text(encoding="utf-8").splitlines()) == line_count


# meiva:note
# Drive `main()` against the tmp tree as the linter's repo root, so nothing escapes into the real
# doc graph, and report the effective exit code with stdout.
def _lint_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
    monkeypatch.chdir(tmp_path)
    code = 0
    try:
        _lint.main([])
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, capsys.readouterr().out


# meiva:note
# The band is gone, not merely unreachable: a surviving constant would let the warning come back
# without anyone re-arguing the ceiling.
def test_the_module_keeps_no_headroom_threshold() -> None:
    assert not hasattr(_lint, "WARN_MARGIN")


def test_the_last_character_under_the_ceiling_passes_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _node_of_size(tmp_path, MAX_CHARS)
    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 0, out
    assert "E007" not in out
    assert "W010" not in out
    assert "0 error(s)" in out


# meiva:note
# The sizes that used to sit in the headroom band — including the last character — now pass with no
# output at all.
@pytest.mark.parametrize("headroom", [1, 100, 799])
def test_a_node_below_the_ceiling_reports_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    headroom: int,
) -> None:
    _node_of_size(tmp_path, MAX_CHARS - headroom)
    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 0, out
    assert "E007" not in out
    assert "W010" not in out
    assert "0 warning(s)" in out


def test_one_character_past_the_ceiling_errors_with_intent_and_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    size = MAX_CHARS + 1
    _node_of_size(tmp_path, size)
    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)

    assert code == 1
    assert "E007" in out
    assert f"{size} chars" in out
    assert f"(max {MAX_CHARS})" in out
    for phrase in _INTENT_PHRASES + _REMEDY_PHRASES:
        assert phrase in out.lower(), phrase
    assert "1 error(s)" in out


# meiva:note
# MAX_LINES shares the rule and the message: one line over reports E007 with the same intent, and
# the line immediately at the ceiling does not.
def test_the_line_ceiling_is_the_same_hard_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _node_of_line_count(tmp_path, MAX_LINES)
    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)
    assert code == 0, out
    assert "E007" not in out

    _node_of_line_count(tmp_path, MAX_LINES + 1)
    code, out = _lint_tmp(tmp_path, monkeypatch, capsys)
    assert code == 1, out
    assert "E007" in out
    assert str(MAX_LINES + 1) in out
    for phrase in _INTENT_PHRASES + _REMEDY_PHRASES:
        assert phrase in out.lower(), phrase
