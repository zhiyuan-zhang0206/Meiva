# meiva:note
# `meizel/state/lint_paths.py` unit tests: which explicit targets a scan reads and how a path is
# labelled in a finding. Asserted by value; the two rules that use them are covered end to end by
# `test_lint_no_os_environ.py` and `test_lint_turn_scoped_config.py`.
# meiva:link meizel/docs/meizel.meiva.okf.md

from __future__ import annotations

from pathlib import Path

from meizel.state import lint_paths

_ROOT = Path("/repo")


def test_only_python_targets_are_read() -> None:
    resolved = ["base/a.py", "base/notes.txt", "base/pkg", "tests/test_b.py"]
    assert lint_paths.python_paths(_ROOT, resolved) == [
        _ROOT / "base/a.py",
        _ROOT / "tests/test_b.py",
    ]


def test_no_explicit_target_reads_nothing() -> None:
    assert lint_paths.python_paths(_ROOT, []) == []


def test_a_label_is_relative_inside_the_repository_and_absolute_outside() -> None:
    assert lint_paths.label(_ROOT / "base/mod.py", _ROOT) == "base/mod.py"
    assert lint_paths.label(Path("/elsewhere/mod.py"), _ROOT) == "/elsewhere/mod.py"
