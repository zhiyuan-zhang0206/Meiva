# meiva:note
# What a new test fixture may call a credential: `fixture-`, and nothing else.
#
# GitGuardian reads the shape of a value, not the code around it. On 2026-11-06 it reported a
# password fixture out of `base/log/tests` as a Generic Password -- on a line whose ruff
# suppression (`# noqa: S105`) means nothing to a scanner. A guard that asks a checker to trust an
# author's comment is backwards; the value's prefix is the one thing both a reader and a scanner
# can check. (The offending value is not quoted here: writing it down is what made the scanner
# report it again, one pull request later.)
#
# The scan is textual on purpose: the line that started this was an assignment inside a
# parametrized string, so an AST walk would have missed it.
#
# Its subject is the checked-out repository, like the other `test_lint_*` gates beside it: the
# selector pins those into every candidate set, so this runs on a pull request that changes nothing
# under `tests/` -- which is exactly the pull request that adds a fixture.
#
# It complements the redaction rule: `base/log/redaction.py` masks a URL password, a secret-named
# key and a `*_TOKEN=` assignment on the way to disk, and this keeps a test fixture from being
# mistaken for one of those before it ever reaches a log.
#
# Pre-existing fixtures are in `fixture_secret_baseline.json`, each with the value's `sha256`
# prefix and one of three reasons. The table is a snapshot, not a permission: change the value and
# the entry stops matching, which turns the line red and forces a fresh decision.

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BASELINE = Path(__file__).with_name("fixture_secret_baseline.json")
_PREFIX = "fixture-"
# meiva:note
# The redaction rule's own output. A line asserting `MEIVA_API_TOKEN=***` states what the record
# must contain after masking, so it is not a fixture value at all.
_MASKED = "***"
_REASONS = frozenset({"reference", "format", "ephemeral", "legacy"})
# meiva:note
# `legacy` is a deferral, not a reason: a fixture nobody has got round to renaming. Left uncapped
# the table only grows, which is the shape this guard exists to prevent. The cap is the count the
# table was taken with; it may fall (rename one and drop the entry) and may never rise. A new
# exemption has to say something specific instead -- `reference`, `format` or `ephemeral`.
_LEGACY_CAP = 32
_ASSIGNMENT = re.compile(
    r"""\b([A-Z][A-Z0-9_]*(?:_TOKEN|_PASSWORD|_SECRET))\b\s*[:=]\s*["']?([^"'\s,)]*)"""
)
_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".worktrees", "site-packages"}


def _offenders() -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for path in sorted(_REPO_ROOT.rglob("*.py")):
        relative = path.relative_to(_REPO_ROOT)
        if _SKIP_DIRS & set(relative.parts) or "tests" not in relative.parts:
            continue
        # meiva:note
        # This file states the pattern it forbids, so it matches itself; the guard is not a
        # fixture.
        if path.name == Path(__file__).name:
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            for match in _ASSIGNMENT.finditer(line):
                value = match.group(2)
                if not value or value.startswith(("$", "{", "(", "=", "\\")):
                    continue
                if value.startswith(_MASKED):
                    continue
                found.append((str(relative), match.group(1), value))
    return found


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def test_a_new_test_fixture_naming_a_secret_says_it_is_a_fixture() -> None:
    baseline = json.loads(_BASELINE.read_text(encoding="utf-8"))
    unexplained = [
        f"{path} :: {name} :: {value!r}"
        for path, name, value in _offenders()
        if not value.startswith(_PREFIX)
        and _digest(value)
        not in {entry["sha8"] for entry in baseline.get(path, []) if entry["name"] == name}
    ]

    assert not unexplained, (
        "a test fixture that names a credential must use a value starting with "
        f"{_PREFIX!r}; scanners read the value's shape, not the code around it:\n"
        + "\n".join(unexplained)
    )


# meiva:note
# The table can only exempt a line it can still describe: a reason outside the three known classes
# and a missing digest are how a baseline rots into a list of file names.
def test_every_exemption_carries_a_known_reason_and_the_value_it_was_taken_for() -> None:
    baseline = json.loads(_BASELINE.read_text(encoding="utf-8"))

    for path, entries in baseline.items():
        for entry in entries:
            assert entry.get("reason") in _REASONS, f"{path} :: {entry.get('name')}: unknown reason"
            assert re.fullmatch(r"[0-9a-f]{8}", entry.get("sha8", "")), (
                f"{path} :: {entry.get('name')}: no value digest"
            )


# meiva:note
# The table only shrinks: a renamed fixture removes its entry and lowers the cap in the same
# change, while a new `legacy` line has nowhere to go.
def test_the_deferral_list_never_grows() -> None:
    baseline = json.loads(_BASELINE.read_text(encoding="utf-8"))
    legacy = [
        f"{path} :: {entry['name']}"
        for path, entries in baseline.items()
        for entry in entries
        if entry.get("reason") == "legacy"
    ]

    assert len(legacy) <= _LEGACY_CAP, (
        f"{len(legacy)} `legacy` exemptions, cap {_LEGACY_CAP}: rename the fixture to `fixture-*` "
        "instead of adding one, or name a specific reason (reference/format/ephemeral):\n"
        + "\n".join(legacy)
    )
