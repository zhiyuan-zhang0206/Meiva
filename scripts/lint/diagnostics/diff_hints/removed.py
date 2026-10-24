# meiva:note
# `deleted-test`: a removed test function is lost coverage unless the change says why.
# The hint looks at removed lines only, groups them per file, and does not fire for a
# test whose name the same change adds back in a file of the same base name or in the same
# file (a move or a rename, not a deletion); the same name in an unrelated file is a
# different test.
from __future__ import annotations

import re

from scripts.lint.diagnostics.diff_hints.model import FileChange, FileKind, HintId, Roles, Site
from scripts.lint.diagnostics.diff_hints.scope import classify

_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w*)\s*\(")
_NAMES_SHOWN = 5


def _test_names(lines: tuple[str, ...]) -> frozenset[str]:
    return frozenset(m.group(1) for m in map(_TEST_DEF.match, lines) if m)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


# meiva:note
# Per file base name, the test names the change adds to files of that name.
def _added_by_basename(changes: tuple[FileChange, ...]) -> dict[str, frozenset[str]]:
    found: dict[str, set[str]] = {}
    for change in changes:
        found.setdefault(_basename(change.path), set()).update(_test_names(change.added_text))
    return {name: frozenset(names) for name, names in found.items()}


def deleted_tests(changes: tuple[FileChange, ...], roles: Roles) -> list[Site]:
    added = _added_by_basename(changes)
    sites: list[Site] = []
    for change in changes:
        if classify(change.path, roles) is not FileKind.TEST:
            continue
        gone = [
            (removed.old_line, match.group(1))
            for removed in change.removed
            if (match := _TEST_DEF.match(removed.text))
            and match.group(1) not in added.get(_basename(change.path), frozenset())
        ]
        if gone:
            shown = ", ".join(name for _, name in gone[:_NAMES_SHOWN])
            more = f" and {len(gone) - _NAMES_SHOWN} more" if len(gone) > _NAMES_SHOWN else ""
            detail = f"{len(gone)} test(s) removed: {shown}{more}"
            sites.append(Site(HintId.DELETED_TEST, change.path, gone[0][0], detail))
    return sites
