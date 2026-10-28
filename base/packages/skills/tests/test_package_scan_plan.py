# meiva:note
# What the install-time package scan decides about a package as a whole, asserted as values: the
# order of its findings and which paths are package content. No file is read; `test_scan.py` scans
# real package trees.

from __future__ import annotations

import os
import stat

import pytest

from base.packages.skills.package_scan_plan import package_file_size, sort_findings
from base.packages.skills.scan import Finding


def test_findings_come_critical_first_then_by_file_line_and_rule() -> None:
    notice_a = Finding("b-rule", "notice", "why", "a.txt", 1, "")
    critical_b = Finding("z-rule", "critical", "why", "b.txt", 9, "")
    critical_a = Finding("z-rule", "critical", "why", "a.txt", 2, "")
    critical_a_first = Finding("a-rule", "critical", "why", "a.txt", 2, "")

    ordered = sort_findings([notice_a, critical_b, critical_a, critical_a_first])

    assert ordered == [critical_a_first, critical_a, critical_b, notice_a]


def _stat(mode: int, size: int = 7) -> os.stat_result:
    return os.stat_result((mode, 1, 1, 1, 0, 0, size, 0, 0, 0, 0.0, 0.0, 0.0, 0, 0, 0))


_REGULAR = _stat(stat.S_IFREG | 0o644, size=123)


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        (("SKILL.md",), 123),
        (("scripts", "run.sh"), 123),
        ((".git", "config"), None),
        (("scripts", "__pycache__", "x.pyc"), None),
        (("node_modules", "left-pad", "index.js"), None),
        ((".venv", "bin", "python"), None),
        (("docs", ".DS_Store"), None),
        (("not.git", "x"), 123),
    ],
)
def test_vcs_and_build_directories_are_not_package_files(
    parts: tuple[str, ...], expected: int | None
) -> None:
    assert package_file_size(parts, _REGULAR) == expected


@pytest.mark.parametrize(
    "st",
    [
        None,
        _stat(stat.S_IFDIR | 0o755),
        _stat(stat.S_IFLNK | 0o777),
        _stat(stat.S_IFSOCK | 0o600),
    ],
)
def test_only_a_regular_file_is_a_package_file(st: os.stat_result | None) -> None:
    assert package_file_size(("SKILL.md",), st) is None


def test_findings_are_ordered_by_path_then_line_then_rule() -> None:
    a2 = Finding("r", "notice", "why", "a.txt", 2, "")
    a10 = Finding("r", "notice", "why", "a.txt", 10, "")
    b1 = Finding("r", "notice", "why", "b.txt", 1, "")
    a2_other_rule = Finding("q", "notice", "why", "a.txt", 2, "")

    assert sort_findings([b1, a10, a2, a2_other_rule]) == [a2_other_rule, a2, a10, b1]
