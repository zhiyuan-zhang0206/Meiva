# meiva:note
# The shell of the install-time package scan: it walks a skill package's tree, `lstat`s and reads
# each entry, and hands what it read to `scan.scan_file`, which decides what the file yields. Which
# entries are package files and the order of the findings are decided in `package_scan_plan.py`,
# whether a file is read and what a file past the size limit, a binary or an undecodable file
# reports in `scan.py`.

from __future__ import annotations

import os
from pathlib import Path

from base.packages.skills.package_scan_plan import package_file_size, sort_findings
from base.packages.skills.scan import Finding, needs_bytes, scan_file


# meiva:note
# The entry's `lstat`, or None when it cannot be stat-ed.
def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except OSError:
        return None


# meiva:note
# Every finding in the package tree at `root`, critical first.
def scan_package(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        size = package_file_size(rel.parts, _lstat_or_none(p))
        if size is None:
            continue
        raw = p.read_bytes() if needs_bytes(size) else None
        findings += scan_file(rel.as_posix(), size, raw)
    return sort_findings(findings)
