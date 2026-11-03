"""`debt carry`: recorded debt follows a file that git sees as renamed, and a symbol moved by hand.

A baseline names files, so moving a file leaves its entries behind (paid, but not removed) and the
violations at the new place unrecorded. Carrying relocates the entries; it never adds one, never
raises a total, and records nothing that was not already recorded.
"""

from __future__ import annotations

import sys
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Protocol

from meizel.contracts.analysis import analyze
from meizel.contracts.debt import (
    EDGE_CODES,
    PATCH_CODES,
    DebtEntry,
    DebtTable,
    file_key,
    total,
)
from meizel.contracts.debt_edit import (
    Naming,
    created_notice,
    read_baselines,
    record,
    resolve,
    write_baseline,
)
from meizel.contracts.debt_edit import prune as prune_debt
from meizel.contracts.debt_report import by_target, diff, manifest_texts_at, summarize, totals_at
from meizel.contracts.debt_rules import (
    declarations_changed,
    measurement_lines,
    rule_change,
    rule_diff,
)
from meizel.contracts.debt_sites import (
    approved,
    format_approved,
    format_sites,
    propagation,
    sites,
)
from meizel.contracts.debt_symbol import (
    SymbolMove,
    declared_manifests,
    defines,
    parse_symbol_move_lines,
    parse_symbol_moves,
    symbol_debt,
)
from meizel.contracts.rule import project
from meizel.graph.model import Code, Fact, FactKind, MeizelError
from meizel.graph.universe import MANIFEST, Checkout, Universe, parent_of

__all__ = ["carry", "run_debt"]

_SEPARATOR = " -> "


def _join(owner: str, path: str) -> str:
    return f"{owner}/{path}" if owner else path


def default_base(repo: Checkout) -> str:
    """The branch point with origin/main, or HEAD when there is no origin/main."""
    return repo.default_base()


def renames_since(repo: Checkout, base: str) -> dict[str, str]:
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


def parse_renames(specs: Sequence[str]) -> list[tuple[str, str]]:
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
    repo: Checkout, base: str, universe: Universe, move: SymbolMove
) -> tuple[bytes, bytes]:
    """The old file at the base and the new file now, after checking that this is a move."""
    if move.new not in universe.files:
        raise MeizelError(
            f"{move.new} is not a file of the repository (missing, or ignored by git)"
        )
    before, after = repo.show_bytes(base, move.old), universe.repo.load_bytes(move.new)
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
    repo: Checkout,
    base: str,
    universe: Universe,
    tables: dict[str, DebtTable],
    moves: list[SymbolMove],
    renames: list[tuple[str, str]],
) -> list[str]:
    manifests = declared_manifests(universe)
    lines: list[str] = []
    for move in moves:
        old_source, new_source = _check_move(repo, base, universe, move)
        before = symbol_debt(universe, manifests, move.old, old_source, move.name)
        after = symbol_debt(universe, manifests, move.new, new_source, move.name)
        lines.extend(_swap(tables, move, before, after, renames))
    return lines


# meiva:note
# Move entries to where their renamed files, renamed patch targets or moved symbols now are: a line per
# entry moved, and the manifests the move had to create — a debt that lands in a directory with none
# cannot be recorded anywhere else, so each of those files is named in the report. `rename_targets`
# (`OLD=NEW`) rewrites the target of a patch entry in the same source file, one for one: a symbol that
# was renamed, not a file. `move_symbols` (`OLD_FILE::NAME=NEW_FILE`) rekeys the entries of a function
# or class that moved from one file to another, which git cannot see as a rename.
def carry(
    repo: Checkout,
    base: str | None = None,
    rename_targets: Sequence[str] | None = None,
    move_symbols: Sequence[str] | None = None,
) -> tuple[list[str], list[str]]:
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
    created: list[str] = []
    for owner in sorted(tables):
        if tables[owner] != baselines.get(owner, {}):
            path = write_baseline(repo, owner, {c: r for c, r in tables[owner].items() if r})
            if path is not None and owner not in universe.manifest_dirs:
                created.append(path)
    return lines, created


# meiva:note
# The two measures' inputs, named: each rule file, manifest declaration and policy entry it edits.
def _measured(
    repo: Checkout,
    ref: str,
    extra: Collection[str],
    changed_decls: list[str] | None = None,
) -> list[str]:
    return [
        "meizel: what this change edits in the measurement:",
        *(f"  {line}" for line in measurement_lines(repo, ref, extra, changed_decls=changed_decls)),
    ]


# meiva:note
# The `Symbol-Move` lines the change declared, read from the record the caller passed. A missing file
# is no declaration at all; a declaration is not proof of anything by itself — `rule_diff` re-checks
# each one against the diff before it retargets an import.
def _declared_symbols(repo: Checkout, path: str | None) -> list[SymbolMove]:
    if not path:
        return []
    # meiva:note
    # Read through the seat that reads every other file: an absolute path is taken as given (the
    # record a CI step writes under the runner's temporary directory), a relative one from the
    # repository root. Reading it here would be a new effect site in a strict module.
    return parse_symbol_move_lines(repo.load_text(path))


# meiva:note
# What `run_debt` reads a mode's shape with. `cli` owns the namespace boundary (`read_args`), and
# importing it back here would make the two modules import each other, so the reader arrives as a
# parameter and every shape below is read off the parsed namespace exactly once.
class ReadArgs(Protocol):
    def __call__[T](self, shape: type[T]) -> T: ...


# meiva:note
# The flags `meizel debt` carries whatever the mode — the subcommand name, the root's worker count,
# and the summary's two flags — which is why they are one base instead of eight copies that drift.
@dataclass(frozen=True)
class DebtBase:
    debt_mode: str | None
    jobs: int | None
    json: bool
    unowned: bool


@dataclass(frozen=True)
class DebtPrune(DebtBase):
    pass


# meiva:note
# The `paths` operand the three mode parsers that take one declare; the summary and the flag-only
# modes never set it, so it is read only for the modes that do.
@dataclass(frozen=True)
class DebtPaths(DebtBase):
    paths: Sequence[str]


# meiva:note
# What a grown baseline costs, printed with the failure: the baseline is the repository's record of
# accepted exceptions, so growth is not a number going up — it is a rule turning into a suggestion.
# The two commands name where the movement sits, and the two documents own the rule.
_GROWTH_GUIDE = (
    "The baseline records exceptions this repository accepted, so it may only shrink: a baseline "
    "that grows turns the rules it covers into suggestions and hands the next author a violation "
    "presented as allowed. Pay the violation down; record it only when the exception is deliberate "
    "and the change explains it. `python -m meizel debt diff --by-target` shows what moved per "
    "target and `python -m meizel debt sites --code <code>` shows where it sits. See "
    "meizel/docs/checker/debt/debt.meiva.okf.md and meizel/docs/checker/debt/diff.meiva.okf.md."
)


@dataclass(frozen=True)
class DebtDiff(DebtBase):
    against: str
    on_growth: str
    by_target: bool
    target_prefix: Sequence[str]
    depth: int
    symbol_moves_file: str | None
    waive_if_changed: Sequence[str]


# meiva:note
# The comparison against `--against`, and what it means when the baseline grew: a rule change is
# reported, never judged, and only the content's own growth is the change's fault.
def _run_diff(repo: Checkout, opts: DebtDiff) -> int:
    ref, on_growth = opts.against, opts.on_growth
    symbols = _declared_symbols(repo, opts.symbol_moves_file)
    base_texts = manifest_texts_at(repo, ref)
    changed_decls = declarations_changed(repo, ref, before=base_texts)
    rules_changed = rule_change(
        repo, ref, opts.waive_if_changed, changed_decls=changed_decls
    ) or bool(symbols)
    if rules_changed:
        result = rule_diff(
            repo,
            ref,
            totals_at(repo, ref, texts=base_texts),
            renames_since(repo, ref),
            opts.jobs,
            symbols,
        )
        print("\n".join(result.lines()))
        print("\n".join(_measured(repo, ref, opts.waive_if_changed, changed_decls)))
        print("\n".join(result.explanation()))
        grew = result.content_grew
    else:
        lines, grew = diff(repo, ref, texts=base_texts)
        print("\n".join(lines))
        print(
            "meizel: totals only: this change edits no rule code, policy or declaration, "
            f"so it has no rules/content split against {ref}"
        )
    if opts.by_target:
        print()
        print("\n".join(by_target(repo, ref, opts.target_prefix, opts.depth)))
        print()
    if not grew:
        if not rules_changed:
            print(f"meizel: no growth against {ref}")
        return 0
    warning = "" if on_growth == "fail" else " warning:"
    print(f"meizel:{warning} recorded debt grew against {ref}. {_GROWTH_GUIDE}", file=sys.stderr)
    return 1 if on_growth == "fail" else 0


@dataclass(frozen=True)
class DebtRecord(DebtBase):
    paths: Sequence[str]
    owner: str
    until: str


def _run_record(repo: Checkout, targets: list[str], opts: DebtRecord, today: str) -> int:
    written, created, unrecordable = record(
        repo, targets, opts.jobs, naming=Naming(opts.owner, opts.until, today)
    )
    for path in written:
        print(f"meizel: wrote {path}")
    if notice := created_notice(created):
        print(notice)
    for diagnostic in unrecordable:
        print(diagnostic.render())
    if unrecordable:
        codes = ", ".join(sorted({d.code.value for d in unrecordable}))
        print(
            f"meizel: {len(unrecordable)} violations cannot be recorded [{codes}]", file=sys.stderr
        )
        return 1
    return 0


@dataclass(frozen=True)
class DebtResolve(DebtBase):
    paths: Sequence[str]


@dataclass(frozen=True)
class DebtSites(DebtBase):
    code: str
    target_prefix: Sequence[str]
    as_json: bool
    paths: Sequence[str]


@dataclass(frozen=True)
class DebtApproved(DebtBase):
    as_json: bool


@dataclass(frozen=True)
class DebtPropagation(DebtBase):
    top: int


@dataclass(frozen=True)
class DebtCarry(DebtBase):
    rename_target: Sequence[str]
    move_symbol: Sequence[str]
    base: str | None


# meiva:note
# What `debt carry` says: the entries it moved, each manifest the move had to create, and — when
# nothing moved — that no rename had recorded debt at all.
def _run_carry(repo: Checkout, opts: DebtCarry) -> int:
    lines, created = carry(repo, opts.base, opts.rename_target, opts.move_symbol)
    for line in lines:
        print(f"meizel: carried {line}")
    for path in created:
        print(f"meizel: created {path}")
    if notice := created_notice(created):
        print(notice)
    if not lines:
        print("meizel: nothing to carry: no renamed file, target or moved symbol has recorded debt")
    return 0


# meiva:note
# The summary's two flags belong to `meizel debt` alone: a mode that received them was asked for two
# different commands at once, so it refuses rather than running one of them. `propagation` is not one
# of those modes: `meizel debt --json propagation` printed the report and never looked at the flag
# before this reader existed, and refusing there would be a new decision rather than a read-through.
def _reject_summary_flags(opts: DebtBase) -> None:
    for flag, asked in (("json", opts.json), ("unowned", opts.unowned)):
        if asked:
            raise MeizelError(f"--{flag} belongs to the summary: `meizel debt --{flag}`")


# meiva:note
# Which debt command runs: every mode names the shape it is read into, and a mode with no shape is a
# new subcommand that arrived without one — refused rather than handed an empty carrier. The
# propagation report is answered before the summary flags are judged: that is the order the command
# already had, so `debt --json propagation` keeps printing the report instead of starting to refuse.
def run_debt(repo: Checkout, targets: list[str], read: ReadArgs, today: str = "") -> int:
    base = read(DebtBase)
    if base.debt_mode is None:
        print(summarize(repo, as_json=base.json, unowned_list=base.unowned))
        return 0
    if base.debt_mode == "propagation":
        opts = read(DebtPropagation)
        analysis = analyze(repo.universe(), jobs=opts.jobs)
        print(
            propagation(analysis.universe, analysis.manifests, analysis.scans).render(top=opts.top)
        )
        return 0
    _reject_summary_flags(base)
    match base.debt_mode:
        case "prune":
            opts = read(DebtPrune)
            for line in prune_debt(repo, opts.jobs):
                print(line)
            return 0
        case "sites":
            opts = read(DebtSites)
            universe = repo.universe()
            found = sites(universe, opts.code, opts.target_prefix, targets, opts.jobs)
            print(format_sites(found, as_json=opts.as_json))
            return 0
        case "approved":
            opts = read(DebtApproved)
            print(format_approved(approved(repo.universe(), opts.jobs), as_json=opts.as_json))
            return 0
        case "resolve":
            opts = read(DebtResolve)
            for line in resolve(repo, targets, opts.jobs):
                print(line)
            return 0
        case "diff":
            return _run_diff(repo, read(DebtDiff))
        case "carry":
            return _run_carry(repo, read(DebtCarry))
        case "record":
            return _run_record(repo, targets, read(DebtRecord), today)
        case unknown:
            raise MeizelError(f"unknown debt mode {unknown!r}")
