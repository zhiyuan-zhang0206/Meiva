"""Reconcile what the rules found with what each directory has recorded as debt.

A directory with an empty baseline is strict: every violation fails. A directory with recorded
debt is strict-to-be: recorded violations pass, a new one fails, and a recorded debt that no
longer occurs fails too, so the baseline can only shrink.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

from meizel.contracts.debt import parse_debt, set_debt, source_of, total
from meizel.contracts.manifest import Manifest
from meizel.graph.model import Code, Diagnostic

__all__ = ["Tally", "debts_in", "reconcile", "resolve_debt_conflicts", "tally"]

type Tally = dict[tuple[str, Code, str], list[Diagnostic]]


def tally(diagnostics: Iterable[Diagnostic]) -> Tally:
    """The violations that can be recorded, grouped by owner directory, code and key."""
    groups: Tally = defaultdict(list)
    for diagnostic in diagnostics:
        if diagnostic.debt is not None:
            groups[(diagnostic.debt.owner, diagnostic.code, diagnostic.debt.key)].append(diagnostic)
    return groups


def _excess(
    found: list[Diagnostic], recorded: int, owner: str, manifests: dict[str, Manifest]
) -> list[Diagnostic]:
    manifest = manifests.get(owner)
    notes: list[str] = []
    if recorded:
        notes.append(f"{recorded} recorded, {len(found)} found")
    if manifest is not None and manifest.debt:
        notes.append(manifest.owner_hint())
    return [replace(d, hints=(*d.hints, *notes)) for d in found]


_WHOLE_REPO_CODES = frozenset({Code.STALE_EXPORT, Code.DOMAIN_SUBSET_LEAK})


def _paid(
    manifest: Manifest,
    groups: Tally,
    in_scope: Callable[[str], bool],
) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    for code, rows in sorted(manifest.debt.items(), key=lambda item: str(item[0])):
        if code in _WHOLE_REPO_CODES and not in_scope(""):
            continue
        for key, recorded in sorted(rows.items()):
            source = source_of(manifest.directory, code, key)
            remaining = len(groups.get((manifest.directory, code, key), ()))
            if remaining >= recorded or not in_scope(source):
                continue
            found.append(
                Diagnostic(
                    manifest.path,
                    manifest.debt_line(code, key),
                    Code.DEBT_PAID,
                    f"{code} debt {key!r}: {recorded} recorded but {remaining} remain",
                    ("run `python -m meizel debt prune` to lower the baseline",),
                )
            )
    return found


def reconcile(
    diagnostics: list[Diagnostic],
    manifests: dict[str, Manifest],
    in_scope: Callable[[str], bool] = lambda _: True,
) -> list[Diagnostic]:
    """The diagnostics that fail `check`.

    `in_scope(path)` says whether a file was analysed. A recorded debt about a file that was not
    analysed cannot be called paid, so a partial run leaves it alone.
    """
    groups = tally(diagnostics)
    failing = [d for d in diagnostics if d.debt is None]
    for (owner, code, key), found in groups.items():
        manifest = manifests.get(owner)
        recorded = manifest.debt.get(code, {}).get(key, 0) if manifest is not None else 0
        if len(found) > recorded:
            failing.extend(_excess(found, recorded, owner, manifests))
    for manifest in manifests.values():
        failing.extend(_paid(manifest, groups, in_scope))
    return failing


def debts_in(manifests: dict[str, Manifest]) -> int:
    """How many debts are recorded across `manifests`."""
    return sum(total(m.debt) for m in manifests.values())


_DEBT_TABLE = re.compile(r"^\[\s*debt\.([^\]\s]+)\s*\]\s*(?:#.*)?$")
_ANY_TABLE = re.compile(r"^\s*\[")
_ENTRY = re.compile(r'^("(?:[^"\\]|\\.)*")\s*=\s*(\d+)\s*(?:#.*)?$')

type _Entries = dict[tuple[str, str], int]


@dataclass(frozen=True, slots=True)
class _Hunk:
    line: int
    table: str | None
    ours: tuple[str, ...]
    theirs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Scan:
    plain: list[str]
    outside: _Entries
    hunks: list[_Hunk]


# meiva:note
# A line outside every hunk, given the current `[debt.<code>]` table: a header moves it; inside a
# debt table an entry is kept as an entry and the rest is dropped, since the tables are written again;
# anywhere else the line is plain text. Returns the table the next line is in.
def _outside(line: str, table: str | None, plain: list[str], outside: _Entries) -> str | None:
    header = _DEBT_TABLE.match(line)
    if header:
        return header.group(1)
    if _ANY_TABLE.match(line):
        plain.append(line)
        return None
    entry = _ENTRY.match(line.strip())
    if table is None:
        plain.append(line)
    elif entry:
        outside[(table, json.loads(entry.group(1)))] = int(entry.group(2))
    return table


_MARKERS = {"<<<<<<<": "ours", "|||||||": "base", "=======": "theirs", ">>>>>>>": ""}


# meiva:note
# The hunk part a marker line starts ("" after the closing marker), or None for any other line. A
# marker counts only inside a hunk, except the opening one.
def _after_marker(line: str, mode: str) -> str | None:
    mark = line[:7]
    return _MARKERS[mark] if mark in _MARKERS and (mode or mark == "<<<<<<<") else None


# meiva:note
# The text outside the debt tables and the conflict hunks, the debt entries outside the hunks, and
# the hunks with the table each starts in. A `|||||||` base section (diff3 style) is dropped: only the
# two sides are compared. After a hunk the current table is the last one its first side names.
def _scan(lines: list[str]) -> _Scan:
    scan = _Scan([], {}, [])
    table: str | None = None
    mode, start = "", 0
    ours: list[str] = []
    theirs: list[str] = []
    for number, line in enumerate(lines, 1):
        after = _after_marker(line, mode)
        if after is None:
            if not mode:
                table = _outside(line, table, scan.plain, scan.outside)
            elif mode in ("ours", "theirs"):
                (ours if mode == "ours" else theirs).append(line)
            continue
        if after == "ours":
            start, ours, theirs = number, [], []
        elif after == "":
            scan.hunks.append(_Hunk(start, table, tuple(ours), tuple(theirs)))
            named = [m.group(1) for m in map(_DEBT_TABLE.match, ours) if m]
            table = named[-1] if named else table
        mode = after
    return scan


# meiva:note
# The `[debt.<code>]` entries one side of a hunk holds, or the line that is something else. The side
# starts in `table`; a header inside the side moves it.
def _entries(side: tuple[str, ...], table: str | None) -> tuple[_Entries, str | None]:
    found: _Entries = {}
    for text in side:
        line = text.strip()
        if not line or line.startswith("#"):
            continue
        header = _DEBT_TABLE.match(line)
        entry = _ENTRY.match(line)
        if header:
            table = header.group(1)
        elif entry and table is not None:
            found[(table, json.loads(entry.group(1)))] = int(entry.group(2))
        else:
            return found, line
    return found, None


# meiva:note
# A manifest with merge conflicts inside its `[debt.<code>]` tables, resolved: the text without the
# markers, and what could not be. Recorded debt is derived, so both sides' entries are kept (a count
# both sides changed becomes the lower one) and `debt prune` lowers them to what still occurs; a
# violation that leaves is a decision for `debt record`, never a number silently raised. A conflict
# anywhere else in the file (purity, roots, uses, exports, state) is reported and left for a person.
def resolve_debt_conflicts(text: str, where: str) -> tuple[str, list[str]]:
    scan = _scan(text.splitlines())
    if not scan.hunks:
        return text, []
    problems: list[str] = []
    merged = dict(scan.outside)
    for hunk in scan.hunks:
        ours, odd = _entries(hunk.ours, hunk.table)
        theirs, odd_theirs = _entries(hunk.theirs, hunk.table)
        if odd is not None or odd_theirs is not None:
            problems.append(
                f"{where}:{hunk.line}: the conflict is not inside the [debt] tables "
                f"(`{odd or odd_theirs}`); decide it by hand"
            )
            continue
        for key in ours.keys() | theirs.keys():
            merged[key] = min(ours.get(key, theirs.get(key, 0)), theirs.get(key, ours.get(key, 0)))
    if problems:
        return text, problems
    raw: dict[str, object] = {}
    for (code, key), count in merged.items():
        rows = raw.setdefault(code, {})
        if isinstance(rows, dict):
            rows[key] = count
    reported: list[str] = []
    table = parse_debt(raw, reported.append)
    if reported:
        return text, [f"{where}: {message}" for message in reported]
    return set_debt("\n".join(scan.plain) + "\n", table), []
