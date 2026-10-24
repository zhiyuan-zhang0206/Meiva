# meiva:note
# `deleted-test`: a removed test function is lost coverage unless the change says why.
# The hint looks at removed lines only, groups them per file, and does not fire for a
# test whose name the same change adds back (a move, not a deletion).
from __future__ import annotations

import re

from scripts.lint.diagnostics.diff_hints.model import FileChange, FileKind, HintId, Roles, Site
from scripts.lint.diagnostics.diff_hints.scope import classify

_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w*)\s*\(")
_NAMES_SHOWN = 5


def _test_names(lines: tuple[str, ...]) -> frozenset[str]:
    return frozenset(m.group(1) for m in map(_TEST_DEF.match, lines) if m)


def deleted_tests(changes: tuple[FileChange, ...], roles: Roles) -> list[Site]:
    re_added = _test_names(tuple(text for change in changes for text in change.added_text))
    sites: list[Site] = []
    for change in changes:
        if classify(change.path, roles) is not FileKind.TEST:
            continue
        gone = [
            (removed.old_line, match.group(1))
            for removed in change.removed
            if (match := _TEST_DEF.match(removed.text)) and match.group(1) not in re_added
        ]
        if gone:
            shown = ", ".join(name for _, name in gone[:_NAMES_SHOWN])
            more = f" and {len(gone) - _NAMES_SHOWN} more" if len(gone) > _NAMES_SHOWN else ""
            detail = f"{len(gone)} test(s) removed: {shown}{more}"
            sites.append(Site(HintId.DELETED_TEST, change.path, gone[0][0], detail))
    return sites
