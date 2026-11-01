"""The read-only baseline summary and the totals-only part of `debt diff`.

These views read `meizel.toml` files and nothing else, so they never analyse the source and finish
in about a second. Rule changes use the separate regeneration in `debt_rules`.
"""

from __future__ import annotations

import json
import tomllib
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any, TypeGuard

from meizel.contracts.debt import PATCH_CODES, TABLE_KEY, count_of, total, unowned
from meizel.contracts.debt_edit import Baselines, read_baselines
from meizel.contracts.debt_sites import target_under
from meizel.contracts.manifest import manifest_path
from meizel.graph.model import Code, MeizelError
from meizel.graph.universe import MANIFEST, Checkout, Universe, parent_of

__all__ = [
    "Recorded",
    "by_target",
    "diff",
    "manifest_texts_at",
    "recorded_when_merged",
    "summarize",
    "totals_at",
]

# meiva:note
# What one tree records, entry by entry: owner directory -> code -> key -> count. `Baselines` carries
# the owner and the reason each entry waits for; the views here compare counts, and a ref may record a
# code the checker has since retired, which `Code` cannot spell.
Recorded = dict[str, dict[str, dict[str, int]]]


def _top(directory: str) -> str:
    return directory.split("/", 1)[0] if directory else "."


def _by_code(baselines: Baselines) -> Counter[Code]:
    counts: Counter[Code] = Counter()
    for table in baselines.values():
        for code, rows in table.items():
            counts[code] += sum(entry.count for entry in rows.values())
    return counts


def _judged_directories(universe: Universe) -> set[str]:
    """Directories with Python anywhere below them: the ones whose rules can be violated."""
    judged: set[str] = set()
    for path in (*universe.python_files, *universe.typescript_files):
        directory = parent_of(path)
        while True:
            judged.add(directory)
            if not directory:
                break
            directory = parent_of(directory)
    return judged


def _report(universe: Universe, *, unowned_list: bool = False) -> dict[str, Any]:
    baselines = read_baselines(universe)
    tops: dict[str, dict[str, Any]] = {}
    for directory, table in baselines.items():
        top = tops.setdefault(
            _top(directory),
            {
                "total": 0,
                "by_code": Counter(),
                "strict_to_be_directories": 0,
                "unowned_entries": 0,
                "unowned": 0,
            },
        )
        top["total"] += total(table)
        top["strict_to_be_directories"] += 1
        entries, occurrences = unowned(table)
        top["unowned_entries"] += entries
        top["unowned"] += occurrences
        for code, rows in table.items():
            top["by_code"][code.value] += sum(entry.count for entry in rows.values())
    judged = _judged_directories(universe)
    missing, occurrences = _unowned(baselines)
    listed = {"unowned_list": _unowned_lines(baselines)} if unowned_list else {}
    return {
        **listed,
        "basis": "recorded manifest debt only; no source, semantic purity, or test-resource analysis",
        "total": sum(total(t) for t in baselines.values()),
        "unowned": {"entries": missing, "occurrences": occurrences},
        "by_code": {c.value: n for c, n in sorted(_by_code(baselines).items(), key=str)},
        "by_top_level": {
            name: {**top, "by_code": dict(sorted(top["by_code"].items()))}
            for name, top in sorted(tops.items())
        },
        "directories": {
            "strict_to_be": len(baselines),
            "strict": len(judged - set(baselines)),
        },
    }


# meiva:note
# A count with its noun in the number the count takes: the summary says "1 recorded debt names no
# owner", not "1 recorded debts name no owner".
def _counted(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _text(data: dict[str, Any]) -> str:
    modes = data["directories"]
    lines = [
        f"meizel debt: {_counted(data['total'], 'recorded locality debt', 'recorded locality debts')}"
        f" in {_counted(modes['strict_to_be'], 'directory', 'directories')} with recorded debt "
        f"({_counted(modes['strict'], 'directory', 'directories')} without recorded debt)",
        data["basis"],
        "",
        (
            "every recorded debt names an owner"
            if not data["unowned"]["entries"]
            else f"{_counted(data['unowned']['entries'], 'recorded debt', 'recorded debts')} "
            f"{'names' if data['unowned']['entries'] == 1 else 'name'} no owner "
            f"({_counted(data['unowned']['occurrences'], 'occurrence', 'occurrences')}); "
            "`meizel debt --unowned` lists them"
        ),
        "",
        "by code:",
        *(f"  {code:<20}{count:>8}" for code, count in data["by_code"].items()),
        "",
        "by top-level directory:",
    ]
    ranked = sorted(data["by_top_level"].items(), key=lambda item: (-item[1]["total"], item[0]))
    lines.extend(
        f"  {name:<20}{top['total']:>8}  in "
        f"{_counted(top['strict_to_be_directories'], 'directory', 'directories')}"
        for name, top in ranked
    )
    listed = data.get("unowned_list")
    if listed:
        lines.extend(["", "recorded debts with no owner:", *listed])
    return "\n".join(lines)


# meiva:note
# How many recorded debts name no owner: the entries a backfill has to reach, and the occurrences
# they carry. A bare count and an entry without `owner` count the same here.
def _unowned(baselines: Baselines) -> tuple[int, int]:
    entries = occurrences = 0
    for table in baselines.values():
        missing, counts = unowned(table)
        entries += missing
        occurrences += counts
    return entries, occurrences


# meiva:note
# Every recorded debt that names no owner, one line each: what a backfill pass reads instead of
# grepping the entries by hand.
def _unowned_lines(baselines: Baselines) -> list[str]:
    lines: list[str] = []
    for directory, table in sorted(baselines.items()):
        for code, rows in sorted(table.items(), key=lambda item: str(item[0])):
            for key, entry in sorted(rows.items()):
                if not entry.owner:
                    lines.append(f"{manifest_path(directory)}: {code} {key} ({entry.count})")
    return lines


def summarize(repo: Checkout, *, as_json: bool, unowned_list: bool = False) -> str:
    data = _report(repo.universe(), unowned_list=unowned_list)
    return json.dumps(data, indent=2, sort_keys=True) if as_json else _text(data)


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_table(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(k, str) for k in value)


# meiva:note
# Every entry one manifest text records, read leniently: a ref is history, and it may record debt
# under a code the tool has since retired without stopping the comparison.
def _cells_in(text: str) -> dict[str, dict[str, int]]:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}
    table = document.get(TABLE_KEY)
    if not _is_table(table):
        return {}
    return {
        code: {key: count_of(n) for key, n in rows.items()}
        for code, rows in table.items()
        if _is_table(rows)
    }


def _totals_in(text: str) -> Counter[str]:
    """Recorded counts per code in one manifest text, read leniently.

    A ref is history: it may record debt under a code the tool has since retired, and that must
    still count toward its total instead of stopping the comparison.
    """
    return Counter({code: sum(rows.values()) for code, rows in _cells_in(text).items()})


# meiva:note
# The text of every `meizel.toml` at a git ref, by repository path.
def manifest_texts_at(repo: Checkout, ref: str) -> dict[str, str]:
    if ref.startswith("-"):
        raise MeizelError(f"{ref}: not a git ref")
    listing = repo.run_bytes("ls-tree", "-r", "-z", "--name-only", ref).decode().split("\0")
    paths = [p for p in listing if p.rsplit("/", 1)[-1] == MANIFEST]
    texts: dict[str, str] = {}
    if not paths:
        return texts
    request = "".join(f"{ref}:{p}\n" for p in paths).encode()
    stream = repo.run_bytes("cat-file", "--batch", stdin=request)
    position = 0
    for path in paths:
        end = stream.index(b"\n", position)
        header = stream[position:end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise MeizelError(f"{ref}:{path}: cannot be read")
        size = int(header[2])
        texts[path] = stream[end + 1 : end + 1 + size].decode()
        position = end + 1 + size + 1
    return texts


def totals_at(repo: Checkout, ref: str, *, texts: Mapping[str, str] | None = None) -> Counter[str]:
    totals: Counter[str] = Counter()
    source = manifest_texts_at(repo, ref) if texts is None else texts
    for text in source.values():
        totals.update(_totals_in(text))
    return totals


# meiva:note
# What one tree's baselines record, entry by entry: the working tree keeps the owner and the reason
# each entry waits for, and a merge decides counts, which is all these views compare.
def _recorded(baselines: Baselines) -> Recorded:
    return {
        owner: {
            code.value: {key: entry.count for key, entry in rows.items()}
            for code, rows in table.items()
        }
        for owner, table in baselines.items()
    }


# meiva:note
# What `ref` records, entry by entry. `manifest_texts_at` reads the blobs, so a ref costs no checkout.
def _recorded_at(repo: Checkout, ref: str, *, texts: Mapping[str, str] | None = None) -> Recorded:
    recorded: Recorded = {}
    source = manifest_texts_at(repo, ref) if texts is None else texts
    for path, text in source.items():
        cells = _cells_in(text)
        if cells:
            recorded[parent_of(path)] = cells
    return recorded


def _recorded_totals(recorded: Recorded) -> Counter[str]:
    totals: Counter[str] = Counter()
    for table in recorded.values():
        for code, rows in table.items():
            totals[code] += sum(rows.values())
    return totals


# meiva:note
# The commit this change and `ref` last shared: what the change itself is measured from.
def _common_ancestor(repo: Checkout, ref: str) -> str:
    if ref.startswith("-"):
        raise MeizelError(f"{ref}: not a git ref")
    return repo.run_text("merge-base", "HEAD", ref).strip()


# meiva:note
# What this change would leave recorded on `ref`: an entry the change moved, at the count the change
# gives it; every other entry at the count `ref` records now.
#
# `debt diff` compared the tree with `ref` alone, so a branch that had fallen behind still recorded
# every entry `ref` had paid since it forked, and the report named `ref`'s own files as the change's
# growth. Three trees name the change instead: the commit it shares with `ref` is what it started
# from, so an entry neither side moved is unchanged, and an entry only `ref` moved is `ref`'s payment.
#
# An entry both sides moved keeps the change's own count. Its merged count is settled when the author
# resolves the merge, and until then the change's own number can accuse a change of debt it did not
# add, but it can never hide debt the change did add.
def recorded_when_merged(
    repo: Checkout, ref: str, *, texts: Mapping[str, str] | None = None
) -> Recorded:
    ancestor = _common_ancestor(repo, ref)
    now = _recorded_at(repo, ref, texts=texts)
    ref_sha = repo.run_text("rev-parse", f"{ref}^{{commit}}").strip()
    started = now if ancestor == ref_sha else _recorded_at(repo, ancestor)
    return _merged(started, now, _recorded(read_baselines(repo.universe())))


def _merged(started: Recorded, now: Recorded, change: Recorded) -> Recorded:
    merged: Recorded = {}
    for owner in sorted({*started, *now, *change}):
        table: dict[str, dict[str, int]] = {}
        for code in sorted({c for side in (started, now, change) for c in side.get(owner, {})}):
            was, stood, mine = (
                side.get(owner, {}).get(code, {}) for side in (started, now, change)
            )
            rows: dict[str, int] = {}
            for key in sorted({*was, *stood, *mine}):
                changed = mine.get(key, 0)
                landed = changed if changed != was.get(key, 0) else stood.get(key, 0)
                if landed:
                    rows[key] = landed
            if rows:
                table[code] = rows
        if table:
            merged[owner] = table
    return merged


def diff(
    repo: Checkout, ref: str, *, texts: Mapping[str, str] | None = None
) -> tuple[list[str], bool]:
    """Per-code totals against `ref`, and whether any code grew.

    Totals, not directories, are compared: debt that moves with a file between directories
    leaves them unchanged.
    """
    before = totals_at(repo, ref, texts=texts)
    after = _recorded_totals(recorded_when_merged(repo, ref, texts=texts))
    codes = sorted(set(before) | set(after))
    lines = [f"{'code':<20}{ref[:12]:>14}{'tree':>8}{'change':>8}"]
    grew = False
    for code in codes:
        delta = after[code] - before[code]
        grew = grew or delta > 0
        lines.append(f"{code:<20}{before[code]:>14}{after[code]:>8}{delta:>+8}")
    lines.append(
        f"{'total':<20}{sum(before.values()):>14}{sum(after.values()):>8}{sum(after.values()) - sum(before.values()):>+8}"
    )
    return lines, grew


Targets = Counter[tuple[str, str]]
_OTHER = "(other)"


# meiva:note
# The target of a patch entry `file#form target`.
def _target_of(key: str) -> str:
    return key.partition("#")[2].partition(" ")[2]


def _targets(tables: Iterable[Mapping[str, Mapping[str, object]]]) -> Targets:
    """Recorded patch counts per `(code, target)`, read leniently from `code -> key -> count`."""
    counts: Targets = Counter()
    patch_codes = {code.value for code in PATCH_CODES}
    for table in tables:
        for code, rows in table.items():
            if code in patch_codes:
                for key, value in rows.items():
                    counts[(code, _target_of(key))] += count_of(value)
    return counts


def _group(target: str, prefixes: list[str], depth: int) -> str:
    """The row a target is counted under: its longest listed prefix, or its first `depth` names."""
    if prefixes:
        inside = [p for p in prefixes if target_under(target, p)]
        return max(inside, key=len) if inside else _OTHER
    return ".".join(target.split(".")[:depth])


def _grouped(
    before: Targets, after: Targets, prefixes: list[str], depth: int
) -> dict[tuple[str, str], list[int]]:
    """`(code, group) -> [before, after]`, with a row for every listed prefix."""
    groups: dict[tuple[str, str], list[int]] = {}
    for side, counts in enumerate((before, after)):
        for (code, target), n in counts.items():
            groups.setdefault((code, _group(target, prefixes, depth)), [0, 0])[side] += n
    for prefix in prefixes:
        if not any(group == prefix for _, group in groups):
            groups[("-", prefix)] = [0, 0]
    return groups


def by_target(repo: Checkout, ref: str, prefixes: list[str], depth: int) -> list[str]:
    """Recorded patch debt per target group against `ref`: what a change paid down or added.

    Counts come from the baselines, keyed by the target of each entry, so debt that moves with
    its file stays where it was. Without prefixes the groups are the first `depth` dotted names
    of a target; with them, a target counts under its longest prefix and the rest under `(other)`.
    Unchanged groups are left out, except the listed prefixes, which always show.
    """
    before = _targets([_cells_in(text) for text in manifest_texts_at(repo, ref).values()])
    after = _targets(recorded_when_merged(repo, ref).values())
    groups = _grouped(before, after, prefixes, depth)
    rows = sorted(
        (item for item in groups.items() if item[1][0] != item[1][1] or item[0][1] in prefixes),
        key=lambda item: (item[1][1] - item[1][0], item[0][1], item[0][0]),
    )
    width = max([len("target"), *(len(group) for (_, group), _ in rows)])
    lines = [f"{'target':<{width}}  {'code':<18}{ref[:12]:>12}{'tree':>8}{'change':>8}"]
    lines.extend(
        f"{group:<{width}}  {code:<18}{old:>12}{new:>8}{new - old:>+8}"
        for (code, group), (old, new) in rows
    )
    old_total, new_total = sum(before.values()), sum(after.values())
    lines.append(
        f"{'all patch debt':<{width}}  {'':<18}{old_total:>12}{new_total:>8}{new_total - old_total:>+8}"
    )
    return lines
