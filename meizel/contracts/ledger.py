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
    has_shape,
    parse_debt,
    read_entry,
    set_debt,
    source_of,
    total,
    unowned,
    unreviewed,
    without_until,
)
from meizel.contracts.manifest import Manifest
from meizel.graph.model import Code, Diagnostic

__all__ = [
    "Tally",
    "account_line",
    "bare_entries",
    "reconcile",
    "resolve_debt_conflicts",
    "tally",
]

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


# meiva:note
# What an `until` answers and the two shapes it takes, printed once per run on the first entry
# that lacks one: the reader who fixes the first entry reads the whole rule, and every later
# entry says in one line what it is missing. Full text on one entry is what keeps twenty of
# them readable instead of a wall of repeated prose.
_UNTIL_EXPLANATION = (
    "`until` answers one question: what has to change before this entry can be deleted.\n"
    "Write one of two shapes, and check it against the code or the installed dependency\n"
    "-- a sentence copied from a neighbour is worth nothing, because the next reader will\n"
    "trust it and stop looking.\n"
    "\n"
    '  until = "waits for <the condition that ends it>: <why it cannot happen yet>"\n'
    "      Something that could arrive. A symbol that becomes an injectable parameter, an\n"
    "      upstream release that adds the seam, another component's conversion that turns\n"
    "      this read into a value, a package move that makes the edge declarable. Name\n"
    "      what you checked today -- the file and function, the installed version's\n"
    "      source, the checker's own rule -- so the next reader re-checks it in one step\n"
    "      instead of redoing the work.\n"
    "\n"
    '  until = "permanently: <the design boundary it is tied to>"\n'
    "      For an entry no reachable change ends: a value only the running process can\n"
    "      answer (its pid, its platform), a name a runtime installs, a shape the\n"
    "      language fixes. Say which boundary, and why no caller inside the process holds\n"
    '      that value better. "Permanently" is a claim the next reader can falsify; make\n'
    "      it specific enough to falsify.\n"
    "\n"
    "Do not invent an `until` to get past this line. An invented one reads like\n"
    "knowledge, and is worse than the bare count it replaces. If you cannot say what\n"
    "would end the entry, that is a finding, not a failure: it usually means the entry is\n"
    "not debt in this sense. Fix it in the same change when the fix is small; otherwise\n"
    "send the evidence -- manifest path, the code it points at, what you checked, what\n"
    "you ruled out -- to the lane that owns the entry, and let it decide. One message\n"
    "beats a wrong sentence that outlives us both."
)

# meiva:note
# What `owner` names and why it is a task rather than the agent working it, printed once per
# run on the first entry that lacks one: the same one-full-text rule as `until`.
_OWNER_EXPLANATION = (
    "`owner` is who re-reads the entry, not who owes it. It names the task that will come\n"
    "back to the `until` and say whether it still holds -- a task lives longer than the\n"
    "agent that works it, and it can be handed over, so the id here is a task id\n"
    "(`#6018`), never an agent id, even though both look like `#1234`.\n"
    "\n"
    "If the entry is leaving in this change, delete it instead of naming an owner for it."
)

# meiva:note
# Why an `until` has two shapes and no third, printed once per run on the first entry whose
# sentence begins with neither: the two answer different questions, and a phrasing that answers
# neither is one a reader skims as if it answered one of them.
_SHAPE_EXPLANATION = (
    "`until` has two shapes because it answers one of two different questions.\\n"
    "\\n"
    '  until = "waits for <the condition that ends it>: <why it cannot happen yet>"\\n'
    "      Something will arrive, and the next reader can check whether it has.\\n"
    "\\n"
    '  until = "permanently: <the design boundary it is tied to>"\\n'
    "      Nothing will arrive, and the next reader can try to falsify that.\\n"
    "\\n"
    "A sentence that begins with neither reads as a reason without saying which of the two it is, and a\\n"
    "reader who cannot tell whether to wait or to argue stops looking. Rewrite it into one of the two.\\n"
    "`unreviewed:` is the marker `debt record` writes and is not a third shape."
)

# meiva:note
# The footer the first bare entry of a run carries: that these are errors, and that an entry
# owned by another task is a message rather than an edit.
_BARE_FOOTER = (
    "These are errors, not warnings: the check fails and CI reports it, so the fix is not\n"
    "optional. If an entry is not in your lane -- `owner` names another task -- do not\n"
    "edit it and do not widen the rule: send one message to that task and leave the entry\n"
    "alone. Two lanes editing one manifest is how edits get lost."
)


# meiva:note
# What is wrong with an entry's metadata, in a fixed order: `owner` first, then `until`, then the shape
# of an `until` that is present but unreadable. One entry can be wrong in more than one way, and the
# message it gets names each; the explanations are handed out per kind, not per entry.
def _missing(entry: DebtEntry) -> tuple[str, ...]:
    faults: list[str] = []
    if not entry.owner:
        faults.append("owner")
    if not entry.until:
        faults.append("until")
    elif not has_shape(entry.until):
        faults.append("shape")
    return tuple(faults)


# meiva:note
# One hint that renders as a block: every line after the first carries the four spaces `render` puts in
# front of a hint, so the explanation reads as one indented passage instead of a paragraph whose tail
# hangs off the left margin.
def _block(text: str) -> str:
    head, *rest = text.splitlines()
    return "\n".join([head, *(f"    {line}" if line else "" for line in rest)])


# meiva:note
# One sentence per entry, saying which of the two fields is missing. A reader who meets twenty of
# them reads twenty facts about twenty entries rather than one fact about the field repeated.
def _bare_message(code: Code, key: str, missing: tuple[str, ...]) -> str:
    if missing == ("shape",):
        return (
            f"{code} {key!r} states an exit condition in no shape this tool reads: `until` must "
            "begin with `waits for ...:` or `permanently:`."
        )
    if set(missing) == {"owner", "until"}:
        return (
            f"{code} {key!r} records debt with no owner and no exit condition: "
            "`owner` and `until` are missing."
        )
    if missing == ("until",):
        return f"{code} {key!r} records debt with no exit condition: `until` is missing."
    if missing == ("owner",):
        return f"{code} {key!r} records debt with no owner."
    return f"{code} {key!r} records debt with no owner and no readable exit condition."


# meiva:note
# Recorded debt that names no owner, states no exit condition, or both: read from the manifest text
# alone, so `check --changed` reports it too -- a run that analysed no source still reads every
# `[debt]` table. The full text goes on the first entry of each missing kind in this run and the
# footer on the first entry overall, so the output grows with the number of entries that are wrong
# and not with the length of the rule.
def bare_entries(manifests: Mapping[str, Manifest]) -> list[Diagnostic]:
    rows: list[tuple[Diagnostic, tuple[str, ...]]] = []
    for _, manifest in sorted(manifests.items()):
        for code, table in sorted(manifest.debt.items(), key=lambda item: str(item[0])):
            for key, entry in sorted(table.items()):
                missing = _missing(entry)
                if missing:
                    diagnostic = Diagnostic(
                        manifest.path,
                        manifest.debt_line(code, key),
                        Code.BARE_DEBT,
                        _bare_message(code, key, missing),
                    )
                    rows.append((diagnostic, missing))
    rows.sort(key=lambda row: (row[0].path, row[0].line, row[0].code, row[0].message))
    found: list[Diagnostic] = []
    until_shown = owner_shown = shape_shown = False
    for index, (diagnostic, missing) in enumerate(rows):
        hints: list[str] = []
        if "until" in missing and not until_shown:
            hints.append(_block(_UNTIL_EXPLANATION))
            until_shown = True
        if "owner" in missing and not owner_shown:
            hints.append(_block(_OWNER_EXPLANATION))
            owner_shown = True
        if "shape" in missing and not shape_shown:
            hints.append(_block(_SHAPE_EXPLANATION))
            shape_shown = True
        if index == 0:
            hints.append(_block(_BARE_FOOTER))
        found.append(replace(diagnostic, hints=tuple(hints)))
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
    failing.extend(bare_entries(manifests))
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
    unstated = unstated_occurrences = 0
    unread = unread_occurrences = 0
    for manifest in manifests.values():
        missing, counts = unowned(manifest.debt)
        entries += missing
        occurrences += counts
        missing, counts = without_until(manifest.debt)
        unstated += missing
        unstated_occurrences += counts
        missing, counts = unreviewed(manifest.debt)
        unread += missing
        unread_occurrences += counts
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
    unstated_line = (
        ""
        if not unstated
        else f"; {_counted(unstated, 'entry', 'entries')} "
        f"({_counted(unstated_occurrences, 'occurrence', 'occurrences')}) "
        f"{'states' if unstated == 1 else 'state'} no exit condition"
    )
    unread_line = (
        ""
        if not unread
        else f"; {_counted(unread, 'entry', 'entries')} "
        f"({_counted(unread_occurrences, 'occurrence', 'occurrences')}) "
        f"{'carries' if unread == 1 else 'carry'} an unreviewed exit condition"
    )
    return carried + ownerless + unstated_line + unread_line


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
