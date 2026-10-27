# meiva:note
# What the install-time package scan decides about a package as a whole, asserted as values: the
# order of its findings and which paths are package content. No file is read; `test_scan.py` scans
# real package trees.

from __future__ import annotations

import pytest

from base.packages.skills.package_scan_plan import is_package_content, sort_findings
from base.packages.skills.scan import Finding


def test_findings_come_critical_first_then_by_file_line_and_rule() -> None:
    notice_a = Finding("b-rule", "notice", "why", "a.txt", 1, "")
    critical_b = Finding("z-rule", "critical", "why", "b.txt", 9, "")
    critical_a = Finding("z-rule", "critical", "why", "a.txt", 2, "")
    critical_a_first = Finding("a-rule", "critical", "why", "a.txt", 2, "")

    ordered = sort_findings([notice_a, critical_b, critical_a, critical_a_first])

    assert ordered == [critical_a_first, critical_a, critical_b, notice_a]


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        (("SKILL.md",), True),
        (("scripts", "run.sh"), True),
        ((".git", "config"), False),
        (("scripts", "__pycache__", "x.pyc"), False),
        (("node_modules", "left-pad", "index.js"), False),
        ((".venv", "bin", "python"), False),
        (("docs", ".DS_Store"), False),
        (("not.git", "x"), True),
    ],
)
def test_vcs_and_build_directories_are_not_package_content(
    parts: tuple[str, ...], expected: bool
) -> None:
    assert is_package_content(parts) is expected
