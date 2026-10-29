"""Reconcile what the rules found with what each directory has recorded as debt.

A directory with an empty baseline is strict: every violation fails. A directory with recorded
debt is strict-to-be: recorded violations pass, a new one fails, and a recorded debt that no
longer occurs fails too, so the baseline can only shrink.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace

from meizel.contracts.debt import (
    DebtEntry,
    entry_value,
    parse_debt,
    read_entry,
    set_debt,
    source_of,
    total,
    unowned,
)
from meizel.contracts.manifest import Manifest
from meizel.graph.model import Code, Diagnostic

__all__ = ["Tally", "account_line", "reconcile", "resolve_debt_conflicts", "tally"]

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
        for key, entry in sorted(rows.items()):
            source = source_of(manifest.directory, code, key)
            remaining = len(groups.get((manifest.directory, code, key), ()))
            if remaining >= entry.count or not in_scope(source):
                continue
            found.append(
                Diagnostic(
                    manifest.path,
                    manifest.debt_line(code, key),
                    Code.DEBT_PAID,
                    f"{code} debt {key!r}: {entry.count} recorded but {remaining} remain",
                    ("run `python -m meizel debt prune` to lower the baseline",),
                )
            )
    return found


# meiva:note
# A read the manifest itself calls legal, recorded as debt: `[roots] name = {kinds = [kind]}` and a
# `[debt.ambient-read]` entry `name#kind target` in the same directory. The two are the same
# declaration read twice, so this needs no source analysis and `check --changed` reports it too —
# unlike `debt-paid`, which is an accounting note about a whole run.
def legal_read_recorded(manifests: Mapping[str, Manifest]) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    for _, manifest in sorted(manifests.items()):
        rows = manifest.debt.get(Code.AMBIENT_READ, {})
        for name, root in sorted(manifest.roots.items()):
            for kind in sorted(root.kinds):
                prefix = f"{name}#{kind.value} "
                found.extend(
                    Diagnostic(
                        manifest.path,
                        manifest.debt_line(Code.AMBIENT_READ, key),
                        Code.LEGAL_READ_RECORDED,
                        f"[roots] {name!r} declares `{kind.value}`, and {key!r} records that read "
                        "as debt",
                        (
                            "delete the entry: one read cannot be licensed by [roots] and paid for "
                            "as debt at once; `debt prune` drops it, and a full check calls it paid "
                            "until then",
                        ),
                    )
                    for key in sorted(rows)
                    if key.startswith(prefix)
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
        entry = manifest.debt.get(code, {}).get(key) if manifest is not None else None
        recorded = entry.count if entry is not None else 0
        if len(found) > recorded:
            failing.extend(_excess(found, recorded, owner, manifests))
    for manifest in manifests.values():
        failing.extend(_paid(manifest, groups, in_scope))
    return failing


# meiva:note
# A count with its noun in the number the count takes: a line that says "1 entry names no owner" reads
# as typed, not assembled.
def _counted(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


# meiva:note
# The account's notes for the ok line: how many occurrences are recorded and across how many
# strict-to-be directories, then how many entries name no owner and how much they carry. Either
# clause disappears when it is zero, so a clean tree's line stays short; the unowned entries are what
# a backfill has to touch.
def account_line(manifests: dict[str, Manifest]) -> str:
    debts = sum(total(m.debt) for m in manifests.values())
    recorded = sum(1 for m in manifests.values() if m.debt)
    entries = occurrences = 0
    for manifest in manifests.values():
        missing, counts = unowned(manifest.debt)
        entries += missing
        occurrences += counts
    carried = (
        ""
        if not debts
        else f"; {_counted(debts, 'recorded debt', 'recorded debts')} "
        f"{'remains' if debts == 1 else 'remain'} in "
        f"{_counted(recorded, 'strict-to-be directory', 'strict-to-be directories')}"
    )
    ownerless = (
        ""
        if not entries
        else f"; {_counted(entries, 'entry', 'entries')} "
        f"({_counted(occurrences, 'occurrence', 'occurrences')}) "
        f"{'names' if entries == 1 else 'name'} no owner"
    )
    return carried + ownerless


_DEBT_TABLE = re.compile(r"^\[\s*debt\.([^\]\s]+)\s*\]\s*(?:#.*)?$")
_ANY_TABLE = re.compile(r"^\s*\[")
_ENTRY = re.compile(r'^("(?:[^"\\]|\\.)*")\s*=\s*(\d+|\{.*\})\s*(?:#.*)?$')

type _Entries = dict[tuple[str, str], DebtEntry]


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
        value, _ = _value(entry.group(2))
        if value is None:
            plain.append(line)
        else:
            outside[(table, json.loads(entry.group(1)))] = value
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
# One side's entry as its line writes it, decoded by the same reader the manifest uses, and the reason
# it is not one when it is not: a bare count or an inline table, and any complaint about it — an
# unknown field, a count that is not positive — refuses the merge. A misspelt `owner` must not read as
# "no owner" here either, so the person resolving the conflict is told which field was wrong.
def _value(raw: str) -> tuple[DebtEntry | None, str]:
    try:
        parsed = tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        return None, "it is not TOML"
    problems: list[str] = []
    entry = read_entry(parsed, "", problems.append)
    return entry, "; ".join(message.removeprefix(": ") for message in problems)


# meiva:note
# What both sides agree one entry is: the lower count, and the metadata either side names — a real
# disagreement about who owns the debt is a decision, so it is refused rather than picked.
def _merged_side(mine: DebtEntry, other: DebtEntry) -> DebtEntry | None:
    for field in ("owner", "until"):
        left, right = getattr(mine, field), getattr(other, field)
        if left and right and left != right:
            return None
    return DebtEntry(
        min(mine.count, other.count), mine.owner or other.owner, mine.until or other.until
    )


# meiva:note
# The `[debt.<code>]` entries one side of a hunk holds, or why a line is not one. The side starts in
# `table`; a header inside the side moves it.
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
            continue
        if not entry or table is None:
            return found, f"`{line}` is not a debt entry"
        key = (table, json.loads(entry.group(1)))
        value, problem = _value(entry.group(2))
        if value is None:
            return found, f"`{line}` is not a debt entry ({problem})"
        previous = found.get(key)
        merged = value if previous is None else _merged_side(previous, value)
        if merged is None:
            return found, f"`{line}` names a different owner or condition than the line before it"
        found[key] = merged
    return found, None


# meiva:note
# Both sides of one hunk as the entries they name, or the first line this command will not decide.
def _hunk_entries(hunk: _Hunk) -> tuple[_Entries, str | None]:
    ours, problem = _entries(hunk.ours, hunk.table)
    theirs, problem_theirs = _entries(hunk.theirs, hunk.table)
    if problem is not None or problem_theirs is not None:
        return {}, problem or problem_theirs
    found: _Entries = {}
    for key in ours.keys() | theirs.keys():
        mine, other = ours.get(key), theirs.get(key)
        if mine is None or other is None:
            kept = mine if mine is not None else other
            if kept is not None:
                found[key] = kept
            continue
        joined = _merged_side(mine, other)
        if joined is None:
            return {}, f"both sides of `{key[1]}` name a different owner or condition"
        found[key] = joined
    return found, None


# meiva:note
# Every hunk merged into the entries the file's `[debt]` tables hold, with the lines to refuse.
def _merge_hunks(scan: _Scan, where: str) -> tuple[_Entries, list[str]]:
    merged = dict(scan.outside)
    problems: list[str] = []
    for hunk in scan.hunks:
        entries, problem = _hunk_entries(hunk)
        if problem is not None:
            problems.append(f"{where}:{hunk.line}: {problem}; decide it by hand")
            continue
        merged.update(entries)
    return merged, problems


# meiva:note
# The merged entries as the raw `[debt]` table `parse_debt` reads back.
def _raw_entries(merged: _Entries) -> dict[str, object]:
    raw: dict[str, object] = {}
    for (code, key), entry in merged.items():
        rows = raw.setdefault(code, {})
        if isinstance(rows, dict):
            rows[key] = entry_value(entry)
    return raw


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
    merged, problems = _merge_hunks(scan, where)
    if problems:
        return text, problems
    reported: list[str] = []
    table = parse_debt(_raw_entries(merged), reported.append)
    if reported:
        return text, [f"{where}: {message}" for message in reported]
    return set_debt("\n".join(scan.plain) + "\n", table), []
