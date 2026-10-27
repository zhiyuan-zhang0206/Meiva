# meiva:note
# What the install-time package scan decides about a package as a whole, from paths and findings:
# which entries are package content and the order of the findings. `package_scan.py` walks the
# tree and `scan.scan_file` decides what each file yields.

from __future__ import annotations

from base.packages.skills.scan import Finding

# meiva:note
# Directories that are never package content.
_SKIP_DIRS = frozenset({".git", "__pycache__", ".DS_Store", "node_modules", ".venv"})


# meiva:note
# Whether a path inside a package is package content, given its parts relative to the package root:
# anything under a VCS, cache or dependency directory is not.
def is_package_content(rel_parts: tuple[str, ...]) -> bool:
    return not any(part in _SKIP_DIRS for part in rel_parts)


# meiva:note
# The findings of a whole package, critical first, then by file, line and rule.
def sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (f.severity != "critical", f.path, f.line, f.rule_id))
