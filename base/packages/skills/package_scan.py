# meiva:note
# The shell of the install-time package scan: it walks a skill package's tree, stats and reads each
# file, and hands what it read to `scan.scan_file`, which decides what the file yields. Which paths
# are package content and the order of the findings are decided in `package_scan_plan.py`, what a
# file past the size limit, a binary or an undecodable file reports in `scan.py`.

from __future__ import annotations

import stat
from pathlib import Path

from base.packages.skills.package_scan_plan import is_package_content, sort_findings
from base.packages.skills.scan import MAX_SCAN_BYTES, Finding, scan_file


# meiva:note
# Every regular file under `root` worth scanning with its size, sorted: symlinks, entries that
# cannot be stat-ed and paths under VCS or build directories are left out.
def _package_files(root: Path) -> list[tuple[Path, int]]:
    out: list[tuple[Path, int]] = []
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        if not is_package_content(p.relative_to(root).parts):
            continue
        out.append((p, st.st_size))
    return out


# meiva:note
# Every finding in the package tree at `root`, critical first.
def scan_package(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for p, size in _package_files(root):
        raw = p.read_bytes() if size <= MAX_SCAN_BYTES else None
        findings += scan_file(p.relative_to(root).as_posix(), size, raw)
    return sort_findings(findings)
