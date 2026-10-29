"""`debt carry`: recorded debt follows a file that git sees as renamed, and a symbol moved by hand.

A baseline names files, so moving a file leaves its entries behind (paid, but not removed) and the
violations at the new place unrecorded. Carrying relocates the entries; it never adds one, never
raises a total, and records nothing that was not already recorded.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Collection

from meizel.contracts.debt import (
    EDGE_CODES,
    PATCH_CODES,
    DebtEntry,
    DebtTable,
    file_key,
    total,
)
from meizel.contracts.debt_edit import prune as prune_debt
from meizel.contracts.debt_edit import record, resolve, write_baseline
from meizel.contracts.debt_report import (
    by_target,
    diff,
    read_baselines,
    summarize,
    totals_at,
)
from meizel.contracts.debt_rules import measurement_lines, rule_change, rule_diff
from meizel.contracts.debt_sites import approved, format_approved, format_sites, sites
from meizel.contracts.debt_symbol import (
    SymbolMove,
    defines,
    parse_symbol_moves,
    symbol_debt,
)
from meizel.contracts.manifest import load_manifests
from meizel.contracts.rule import project
from meizel.graph.model import Code, Fact, FactKind, MeizelError
from meizel.graph.universe import MANIFEST, GitRepo, Universe, parent_of

__all__ = ["carry", "run_debt"]

_SEPARATOR = " -> "


def _join(owner: str, path: str) -> str:
    return f"{owner}/{path}" if owner else path


def default_base(repo: GitRepo) -> str:
    """The branch point with origin/main, or HEAD when there is no origin/main."""
    return repo.default_base()


def renames_since(repo: GitRepo, base: str) -> dict[str, str]:
    """`old -> new` for the files git detects as renamed between `base` and the working tree."""
    if base.startswith("-"):
        raise MeizelError(f"{base}: not a git ref")
    listing = repo.run_text("diff", "-M", "--name-status", "-z", base).split("\0")
    found: dict[str, str] = {}
    index = 0
    while index < len(listing) and listing[index]:
        status = listing[index]
        if status.startswith("R"):
            old, new = listing[index + 1], listing[index + 2]
            if old.rsplit("/", 1)[-1] != MANIFEST and new.rsplit("/", 1)[-1] != MANIFEST:
                found[old] = new
            index += 3
        else:
            index += 2
    return found


# meiva:note
# The directory that owns an edge's debt: the lowest common directory of its two files.
def edge_owner(src: str, dst: str) -> str:
    projection = project(Fact(src, dst, 0, FactKind.IMPORT))
    return projection.lca if projection is not None else parent_of(src)


def _relocated(
    owner: str, code: Code, key: str, moved: dict[str, str], gone: set[str]
) -> tuple[str, str] | None:
    """The `(owner, key)` an entry has after the renames, or None when it is not affected."""
    if code in EDGE_CODES:
        head, _, name = key.partition("#")
        src, _, dst = head.partition(_SEPARATOR)
        old_src, old_dst = _join(owner, src), _join(owner, dst)
        if old_src not in gone and old_dst not in gone:
            return None
        new_src, new_dst = moved.get(old_src, old_src), moved.get(old_dst, old_dst)
        new_owner = edge_owner(new_src, new_dst)
        within = len(new_owner) + 1 if new_owner else 0
        edge = f"{new_src[within:]}{_SEPARATOR}{new_dst[within:]}"
        return new_owner, f"{edge}#{name}" if name else edge
    file, _, what = key.partition("#")
    old = _join(owner, file)
    if old not in gone:
        return None
    new = moved[old]
    return parent_of(new), file_key(parent_of(new), new, what)


_PATCH_CODES = frozenset(PATCH_CODES)


def parse_renames(specs: list[str]) -> list[tuple[str, str]]:
    """`OLD=NEW` target renames, each side a non-empty dotted name."""
    renames: list[tuple[str, str]] = []
    for spec in specs:
        old, _, new = spec.partition("=")
        if not old or not new or old == new:
            raise MeizelError(
                f"{spec!r}: a target rename is written OLD=NEW with two different names"
            )
        renames.append((old, new))
    return renames


def _retargeted(key: str, renames: list[tuple[str, str]]) -> str | None:
    """The patch key with its target renamed (`file#form target`), or None when no rename applies."""
    file, _, what = key.partition("#")
    form, _, target = what.partition(" ")
    for old, new in renames:
        if target == old or target.startswith(f"{old}."):
            return f"{file}#{form} {new}{target[len(old) :]}"
    return None


def _plan(
    baselines: dict[str, DebtTable],
    moved: dict[str, str],
    universe: Universe,
    renames: list[tuple[str, str]],
) -> tuple[dict[str, DebtTable], list[str]]:
    gone = {old for old in moved if old not in universe.files}
    tables = {
        owner: {c: dict(rows) for c, rows in table.items()} for owner, table in baselines.items()
    }
    lines: list[str] = []
    for owner, table in baselines.items():
        for code, rows in table.items():
            for key, entry in sorted(rows.items()):
                target = _relocated(owner, code, key, moved, gone)
                if target is None and code in _PATCH_CODES:
                    renamed = _retargeted(key, renames)
                    target = None if renamed is None else (owner, renamed)
                if target is None or target == (owner, key):
                    continue
                del tables[owner][code][key]
                landing = tables.setdefault(target[0], {}).setdefault(code, {})
                lines.append(f"{code} {_join(owner, key)} => {_join(*target)} ({entry.count})")
                lines.extend(_conflict(code, target, landing.get(target[1]), entry))
                landing[target[1]] = _merged(landing.get(target[1]), entry)
    return tables, lines


# meiva:note
# Two entries on the key a move lands on: the counts add, and the metadata already at the target wins
# where it exists, the moved entry filling in where it does not. Paying attention to which owner is
# kept is the point of the field, so a real disagreement is reported rather than resolved silently.
def _merged(here: DebtEntry | None, moved: DebtEntry) -> DebtEntry:
    if here is None:
        return moved
    return DebtEntry(here.count + moved.count, here.owner or moved.owner, here.until or moved.until)


def _conflict(
    code: Code, target: tuple[str, str], here: DebtEntry | None, moved: DebtEntry
) -> list[str]:
    if here is None or not here.owner or not moved.owner or here.owner == moved.owner:
        return []
    return [f"{code} {_join(*target)}: kept owner {here.owner} over {moved.owner}"]


def _check_move(
    repo: GitRepo, base: str, universe: Universe, move: SymbolMove
) -> tuple[bytes, bytes]:
    """The old file at the base and the new file now, after checking that this is a move."""
    if move.new not in universe.files:
        raise MeizelError(
            f"{move.new} is not a file of the repository (missing, or ignored by git)"
        )
    before, after = repo.show_bytes(base, move.old), universe.load_bytes(move.new)
    if repo.has_file(move.old) and defines(
        universe, move.old, repo.load_bytes(move.old), move.name
    ):
        raise MeizelError(f"{move.old} still defines {move.name}: that is a copy, not a move")
    for path, source, where in (
        (move.old, before, f"{move.old} at {base}"),
        (move.new, after, move.new),
    ):
        if not defines(universe, path, source, move.name):
            raise MeizelError(f"{where} defines no top-level function or class {move.name}")
    return before, after


def _swap(
    tables: dict[str, DebtTable],
    move: SymbolMove,
    before: dict[tuple[Code, str, str], int],
    after: dict[tuple[Code, str, str], int],
    renames: list[tuple[str, str]],
) -> list[str]:
    """Rekey, one for one, the entries the symbol had in the old file to its new file.

    At most what the symbol cost before, what it costs now and what is recorded moves, so a
    violation the symbol gained on the way stays unrecorded.
    """
    lines: list[str] = []
    for (code, owner, recorded), count in sorted(before.items()):
        key = (_retargeted(recorded, renames) if code in _PATCH_CODES else None) or recorded
        landing = _relocated(owner, code, key, {move.old: move.new}, {move.old})
        if landing is None:
            continue
        rows = tables.get(owner, {}).get(code, {})
        entry = rows.get(key)
        amount = min(count, after.get((code, *landing), 0), entry.count if entry else 0)
        if amount == 0 or entry is None:
            continue
        rows[key] = DebtEntry(entry.count - amount, entry.owner, entry.until)
        if not rows[key].count:
            del rows[key]
        row = tables.setdefault(landing[0], {}).setdefault(code, {})
        row[landing[1]] = _merged(row.get(landing[1]), DebtEntry(amount, entry.owner, entry.until))
        lines.append(f"{code} {_join(owner, key)} => {_join(*landing)} ({amount}, {move.name})")
    return lines


def _move_symbols(
    repo: GitRepo,
    base: str,
    universe: Universe,
    tables: dict[str, DebtTable],
    moves: list[SymbolMove],
    renames: list[tuple[str, str]],
) -> list[str]:
    manifests, _ = load_manifests(universe)
    lines: list[str] = []
    for move in moves:
        old_source, new_source = _check_move(repo, base, universe, move)
        before = symbol_debt(universe, manifests, move.old, old_source, move.name)
        after = symbol_debt(universe, manifests, move.new, new_source, move.name)
        lines.extend(_swap(tables, move, before, after, renames))
    return lines


def carry(
    repo: GitRepo,
    base: str | None = None,
    rename_targets: list[str] | None = None,
    move_symbols: list[str] | None = None,
) -> list[str]:
    """Move entries to where their renamed files, renamed patch targets or moved symbols now are.

    Returns a line per entry moved. `rename_targets` (`OLD=NEW`) rewrites the target of a patch
    entry in the same source file, one for one: a symbol that was renamed, not a file.
    `move_symbols` (`OLD_FILE::NAME=NEW_FILE`) rekeys the entries of a function or class that
    moved from one file to another, which git cannot see as a rename.
    """
    base = base or default_base(repo)
    moved = renames_since(repo, base)
    universe = repo.universe()
    baselines = read_baselines(universe)
    renames = parse_renames(rename_targets or [])
    symbols = parse_symbol_moves(move_symbols or [])
    tables, lines = _plan(baselines, moved, universe, renames)
    lines.extend(_move_symbols(repo, base, universe, tables, symbols, renames))
    before = sum(total(t) for t in baselines.values())
    after = sum(total(t) for t in tables.values())
    if after != before:
        raise MeizelError(f"carrying would change the recorded total ({before} -> {after})")
    for owner in sorted(tables):
        if tables[owner] != baselines.get(owner, {}):
            write_baseline(repo, owner, {c: r for c, r in tables[owner].items() if r})
    return lines


# meiva:note
# The two measures' inputs, named: each rule file, manifest declaration and policy entry it edits.
def _measured(repo: GitRepo, ref: str, extra: Collection[str]) -> list[str]:
    return [
        "meizel: what this change edits in the measurement:",
        *(f"  {line}" for line in measurement_lines(repo, ref, extra)),
    ]


def _run_diff(repo: GitRepo, ref: str, on_growth: str, args: argparse.Namespace) -> int:
    rules_changed = rule_change(repo, ref, args.waive_if_changed)
    if rules_changed:
        result = rule_diff(repo, ref, totals_at(repo, ref), renames_since(repo, ref), args.jobs)
        print("\n".join(result.lines()))
        print("\n".join(_measured(repo, ref, args.waive_if_changed)))
        print("\n".join(result.explanation()))
        grew = result.content_grew
    else:
        lines, grew = diff(repo, ref)
        print("\n".join(lines))
    if args.by_target:
        print()
        print("\n".join(by_target(repo, ref, args.target_prefix, args.depth)))
        print()
    if not grew:
        if not rules_changed:
            print(f"meizel: no growth against {ref}")
        return 0
    warning = "" if on_growth == "fail" else " warning:"
    print(f"meizel:{warning} recorded debt grew against {ref}", file=sys.stderr)
    return 1 if on_growth == "fail" else 0


def _run_record(repo: GitRepo, targets: list[str], jobs: int | None) -> int:
    written, created, unrecordable = record(repo, targets, jobs)
    for path in written:
        print(f"meizel: wrote {path}")
    if created:
        print(
            f"meizel: {len(created)} manifests created; stage them with git add, "
            "because check reads tracked files only"
        )
    for diagnostic in unrecordable:
        print(diagnostic.render())
    if unrecordable:
        codes = ", ".join(sorted({d.code.value for d in unrecordable}))
        print(
            f"meizel: {len(unrecordable)} violations cannot be recorded [{codes}]", file=sys.stderr
        )
        return 1
    return 0


def run_debt(repo: GitRepo, targets: list[str], args: argparse.Namespace, jobs: int | None) -> int:
    mode = args.debt_mode
    if mode is None:
        print(summarize(repo, as_json=args.json, unowned_list=args.unowned))
        return 0
    for flag in ("json", "unowned"):
        if getattr(args, flag, False):
            raise MeizelError(f"--{flag} belongs to the summary: `meizel debt --{flag}`")
    if mode == "prune":
        for line in prune_debt(repo, jobs):
            print(line)
        return 0
    if mode in ("sites", "approved"):
        universe = repo.universe()
        if mode == "sites":
            found = sites(universe, args.code, args.target_prefix, targets, jobs)
            print(format_sites(found, as_json=args.as_json))
        else:
            print(format_approved(approved(universe, jobs), as_json=args.as_json))
        return 0
    if mode == "resolve":
        for line in resolve(repo, targets, jobs):
            print(line)
        return 0
    if mode == "diff":
        return _run_diff(repo, args.against, args.on_growth, args)
    if mode == "carry":
        lines = carry(repo, args.base, args.rename_target, args.move_symbol)
        for line in lines:
            print(f"meizel: carried {line}")
        if not lines:
            print(
                "meizel: nothing to carry: no renamed file, target or moved symbol has recorded debt"
            )
        return 0
    return _run_record(repo, targets, jobs)
