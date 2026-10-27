# meiva:note
# What the install-time package scan decides about a package as a whole, from stats, paths and
# findings: which entries are package files and how large they are, and the order of the findings.
# `package_scan.py` walks the tree and `scan.scan_file` decides what each file yields.

from __future__ import annotations

import os

from base.packages.skills.index_plan import FileKind
from base.packages.skills.scan import Finding

# meiva:note
# Directories that are never package content.
_SKIP_DIRS = frozenset({".git", "__pycache__", ".DS_Store", "node_modules", ".venv"})


# meiva:note
# The size of an entry of a package to scan, or None when it is not scanned: it has no stat (it
# vanished), it is not a regular file (a directory, or a symlink as `lstat` describes it), or its
# path, given as parts relative to the package root, runs under a VCS, cache or dependency
# directory.
def package_file_size(rel_parts: tuple[str, ...], st: os.stat_result | None) -> int | None:
    if FileKind.of(st) is not FileKind.REGULAR or st is None:
        return None
    if any(part in _SKIP_DIRS for part in rel_parts):
        return None
    return st.st_size


# meiva:note
# The findings of a whole package, critical first, then by file, line and rule.
def sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (f.severity != "critical", f.path, f.line, f.rule_id))
