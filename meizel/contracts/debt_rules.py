# meiva:note
# `debt diff` for a change that edits the rules: the repayment the content made, apart from the change
# the device made.
#
# A rule change makes the checker find debt that was always in the code, so the recorded total moves
# by design. That movement must not hide a content change that moves it too, and a content change must
# not hide behind it. Three measures over one 2x2 — the device `D` is the rule code, `meizel.policy.toml`
# and the declarations of every `meizel.toml` (everything but its `[debt]` tables); `C` is the content:
#
#                        | the ref's content           | the change's content
#   the ref's device     | the ref's record (`base`)   | REPAID (this change's subject)
#   the change's device  | the found baseline          | the change's record (`tree`)
#
# - Rules: what this checker finds in the BASE tree's content, minus what the base records.
# - Content: what the change records, minus what this checker finds in the base tree's content.
# - Repaid: what the base records, minus what the ref's own declarations find in the change's content.
#   The left term is the ref's RECORD, the same anchor `rules` uses, and for a ref this comparison is
#   meant for — a green one — the record is what its own device finds there.
#
# Repaid is the one measure content cannot be. `Content` subtracts the CHANGE's device from the base's
# content, so deleting a read that the new device no longer measures moves neither of its two terms: a
# change that deletes a read and declares a root for it would read exactly like a change that only
# declares the root, and the repayment would be invisible. Repaid holds the device at the ref, so it
# asks the other question — does the code still do what the old rules measured? (Pull request 8369 is
# the case: two `sys.executable` reads deleted from `bisect_train.py` and its `[roots]` given `pid`
# read as `rules -2 / content +0` alone.)
#
# "What this checker finds in the base tree" is the baseline it would write for the base: this checker
# (the code being judged) runs on a throwaway checkout of the base commit, declaring the legal edges and
# recording the rest, exactly as a rule change's own baseline commit does. "What the ref's own
# declarations find in the change's content" is the same replay with the pair swapped: the ref's
# manifests over a checkout of the change's content, because a checker cannot run another version's code
# and only the manifests are the device it can hold still. The base's recorded debt is the old rule's
# result, since the base is green. Rules are reported and never judged. Content growth is
# judged: every key the change records must be one the regenerated base records, with no greater count,
# so a declaration cannot hide new violations and a violation cannot ride along. The base's content is
# measured at the paths the change uses: a file git reports as renamed is moved in the base before it
# is measured, so a rename is neither a rule change nor growth, for file keys and edge keys alike.
#
# The measuring device is the rule code, `meizel.policy.toml` AND the declarations of every
# `meizel.toml` (everything but its `[debt]` tables: `exports`, `uses`, `loads`, `external`, `roles`,
# `purity`, `state`, `roots`). Regenerating the base therefore writes the HEAD's manifests, minus their
# debt tables, over the base's content: declaring a root, a legal edge or a mode is a change of what is
# measured, never repayment of debt. A change that edits only `[debt]` tables is not a rule change and
# keeps the single-column report.
from __future__ import annotations

import tomllib
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

from meizel.contracts.analysis import Analysis, manifest_diagnostics
from meizel.contracts.debt import CYCLE_EDGE, TABLE_KEY, set_debt, total
from meizel.contracts.debt_edit import Baselines, read_baselines, record
from meizel.contracts.debt_edit import prune as prune_debt
from meizel.contracts.debt_report import Recorded, manifest_texts_at, recorded_when_merged
from meizel.contracts.debt_symbol import SymbolMove, defines, retarget_imports
from meizel.contracts.declare import declare
from meizel.contracts.policy import POLICY, load_policy, policy_at
from meizel.contracts.policy import changes as policy_changes
from meizel.contracts.sync import prepare_policy_snapshot, prune
from meizel.graph.model import Code, Diagnostic, MeizelError
from meizel.graph.universe import MANIFEST, Checkout, EditPlan, Universe
from meizel.language.scan import invalidate_policy_caches

__all__ = ["RuleDiff", "declarations_changed", "measurement_lines", "rule_change", "rule_diff"]

# A manifest or policy this checker cannot read says nothing about the base: counting it as no debt
# would let any rule change that breaks the format pass as clean.
_UNREADABLE = frozenset(
    {
        Code.MANIFEST_SYNTAX,
        Code.MANIFEST_KEY,
        Code.MANIFEST_CHILD,
        Code.MANIFEST_DUPLICATE,
        Code.MANIFEST_DEBT,
        Code.MANIFEST_ROLES,
        Code.POLICY_SYNTAX,
    }
)
_DEBT_ROWS = 5

# meiva:note
# The rule code and the policy: what the checker judges with, apart from the manifests' declarations.
# CI passes the same set as `--waive-if-changed`; `debt diff` detects it by itself now, and the flag
# only adds pathspecs. The excluded paths are the ones that are not the device even though they live
# under `meizel/`: the tests are exempt from the codes the checker records, and the state rules and the
# three allowance readers are measured as content, so a violation the change adds there is caught.
RULE_PATHS = (
    ":(glob)meizel/**/*.py",
    ":(exclude,glob)meizel/**/tests/**",
    ":(exclude,glob)meizel/state/**",
    ":(exclude)meizel/language/path_imports.py",
    ":(exclude)meizel/language/no_script_sibling_imports.py",
    ":(exclude)meizel/allowances.py",
    POLICY,
)

# meiva:note
# The rule code alone: the policy is listed entry by entry in `measurement_lines`, never as a path.
RULE_CODE = tuple(path for path in RULE_PATHS if path != POLICY)


# meiva:note
# The two replays one comparison runs, kept together because they are one decision made twice: the
# change's device over the ref's content (`found`), and the ref's device over the change's content
# (`under_reference`, the corner `repaid` reads). A caller that passed one without the other would be
# reporting a comparison it did not make.
@dataclass(frozen=True, slots=True)
class Replays:
    under_change: Baselines
    under_reference: Baselines


# meiva:note
# The three measures for one change, per code, and the keys that make the content measure fail.
class RuleDiff:
    def __init__(
        self,
        base: Counter[str],
        replays: Replays,
        tree: Recorded,
        ref: str,
        rewritten: Sequence[str] = (),
        ignored: Sequence[str] = (),
    ) -> None:
        self.ref = ref
        self.base = base
        self.found = _totals(replays.under_change)
        self.tree = _count_totals(tree)
        self.repaid = _repaid(base, replays.under_reference)
        self.excess = _excess(tree, replays.under_change)
        self.rewritten = tuple(rewritten)
        self.ignored = tuple(ignored)

    @property
    def content_grew(self) -> bool:
        return bool(self.excess)

    def measures(self) -> tuple[int, int, int]:
        return (
            sum(self.found.values()) - sum(self.base.values()),
            sum(self.tree.values()) - sum(self.found.values()),
            sum(self.repaid.values()),
        )

    def lines(self) -> list[str]:
        codes = sorted(set(self.base) | set(self.found) | set(self.tree))
        out = [
            f"{'code':<20}{self.ref[:12]:>14}{'new rules':>11}{'tree':>8}{'rules':>8}"
            f"{'content':>9}{'repaid':>8}"
        ]
        for code in codes:
            out.append(
                f"{code:<20}{self.base[code]:>14}{self.found[code]:>11}{self.tree[code]:>8}"
                f"{self.found[code] - self.base[code]:>+8}{self.tree[code] - self.found[code]:>+9}"
                f"{self.repaid[code]:>+8}"
            )
        rules, content, repaid = self.measures()
        out.append(
            f"{'total':<20}{sum(self.base.values()):>14}{sum(self.found.values()):>11}"
            f"{sum(self.tree.values()):>8}{rules:>+8}{content:>+9}{repaid:>+8}"
        )
        out.append(
            f"meizel: rules {rules:+d} / content {content:+d} / repaid {repaid:+d} "
            f"against {self.ref[:12]}"
        )
        out.extend(self.rewritten)
        return out

    def explanation(self) -> list[str]:
        if not self.excess:
            return [
                *self.ignored,
                f"meizel: no content growth against {self.ref}: the rules account for the rest",
            ]
        shown = self.excess[:_DEBT_ROWS]
        more = len(self.excess) - len(shown)
        return [
            *self.ignored,
            f"meizel: the change records debt the rules do not find in {self.ref[:12]}'s content:",
            *(
                f"  {where}: {key} ({code}) {have} recorded, {want} found"
                for where, code, key, have, want in shown
            ),
            *([f"  ... and {more} more"] if more else []),
            "meizel: a rule change records only what the rules find in the unchanged code; "
            "move files and change code in their own change",
        ]


def _totals(baselines: Baselines) -> Counter[str]:
    counts: Counter[str] = Counter()
    for table in baselines.values():
        for code, rows in table.items():
            counts[code.value] += total({code: rows})
    return counts


# meiva:note
# What the ref's recorded debt lost to the change's content alone: every code's base count minus what
# the ref's own declarations find in the change's content. A negative number is debt the old device
# finds in the new code and the ref does not record: the change's content does something the old rules
# measured, which is the one thing a reader of `content` alone cannot tell from a rule change.
def _repaid(base: Counter[str], under_reference: Baselines) -> Counter[str]:
    found = _totals(under_reference)
    return Counter({code: base[code] - found[code] for code in set(base) | set(found)})


def _count_totals(recorded: Recorded) -> Counter[str]:
    counts: Counter[str] = Counter()
    for table in recorded.values():
        for code, rows in table.items():
            counts[code] += sum(rows.values())
    return counts


# meiva:note
# Every (directory, code, key) the tree records beyond what the regenerated base records. The base is
# measured at the change's own paths (a moved file is moved before it is measured), so a rename is
# neither a rule change nor growth, and no key needs translating.
def _excess(tree: Recorded, found: Baselines) -> list[tuple[str, str, str, int, int]]:
    out: list[tuple[str, str, str, int, int]] = []
    for owner, table in sorted(tree.items()):
        known = {
            code.value: {key: entry.count for key, entry in rows.items()}
            for code, rows in found.get(owner, {}).items()
        }
        for code, rows in table.items():
            for key, have in sorted(rows.items()):
                want = known.get(code, {}).get(key, 0)
                if have > want:
                    out.append((owner or ".", code, key, have, want))
    return out


def _refuse_unreadable(diagnostics: Sequence[Diagnostic], ref: str) -> None:
    unreadable = [d for d in diagnostics if d.code in _UNREADABLE]
    if unreadable:
        shown = "\n".join(d.render() for d in unreadable[:5])
        raise MeizelError(
            f"this checker cannot read the manifests of {ref}, so it cannot say what the new rules "
            f"find there:\n{shown}"
        )


# meiva:note
# The files that differ between `ref` and the working tree under the given git pathspecs; no
# pathspecs lists every changed file.
def _changed_under(repo: Checkout, ref: str, pathspecs: list[str]) -> list[str]:
    if ref.startswith("-"):
        raise MeizelError(f"{ref}: not a git ref")
    listing = repo.run_bytes("diff", "--name-only", "--no-renames", "-z", ref, "--", *pathspecs)
    return [p for p in listing.decode().split("\0") if p]


# meiva:note
# The declarations of one manifest: everything it states except its `[debt]` tables, which record a
# measurement rather than form the device that makes it.
def _declarations(text: str) -> dict[str, object]:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}
    document.pop(TABLE_KEY, None)
    return document


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_table(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(key, str) for key in value)


_ALL = "*"


# meiva:note
# One declared table's entries; a scalar or a list is a single entry named `*`, because the table is
# the smallest thing a review names for them.
def _entries(value: object) -> dict[str, object]:
    if _is_table(value):
        return dict(value)
    return {_ALL: value}


# meiva:note
# One line per declaration added, removed or changed between `ref` and the working tree, written
# `<manifest> [<table>] <key> <sign>`, so a reviewer reads what the two measures came from.
def declarations_changed(
    repo: Checkout, ref: str, *, before: Mapping[str, str] | None = None
) -> list[str]:
    base_texts = manifest_texts_at(repo, ref) if before is None else before
    files = repo.universe().files
    after = {
        path: repo.load_text(path, default="")
        for path in sorted(files)
        if path.rsplit("/", 1)[-1] == MANIFEST
    }
    lines: list[str] = []
    for path in sorted(base_texts.keys() | after.keys()):
        was = _declarations(base_texts.get(path, ""))
        now = _declarations(after.get(path, ""))
        for table in sorted(was.keys() | now.keys(), key=str):
            old = _entries(was[table]) if table in was else {}
            new = _entries(now[table]) if table in now else {}
            for key in sorted(old.keys() | new.keys(), key=str):
                if key in old and key in new and old[key] == new[key]:
                    continue
                sign = "+" if key not in old else "-" if key not in new else "~"
                lines.append(f"{path} [{table}] {key} {sign}")
    return lines


# meiva:note
# What a change edits in the measuring device, item by item: every rule file and every pathspec the
# caller adds, the policy's own diff for its entries, and one line per manifest declaration; a reviewer
# reads the two measures' inputs instead of taking a total on trust.
def measurement_lines(
    repo: Checkout,
    ref: str,
    extra: Collection[str] = (),
    *,
    changed_decls: Sequence[str] | None = None,
) -> list[str]:
    universe = repo.universe()
    entries = [
        f"{POLICY} {line}"
        for line in policy_changes(policy_at(universe, ref), load_policy(universe)[0])
    ]
    if not entries and POLICY in _changed_under(repo, ref, [POLICY]):
        entries = [POLICY]
    decls = declarations_changed(repo, ref) if changed_decls is None else changed_decls
    return [
        *_changed_under(repo, ref, [*RULE_CODE, *extra]),
        *decls,
        *entries,
    ]


# meiva:note
# Whether the interval changes what is measured, not only what the recorded number says.
def rule_change(
    repo: Checkout,
    ref: str,
    extra: Collection[str] = (),
    *,
    changed_decls: Sequence[str] | None = None,
) -> bool:
    decls = declarations_changed(repo, ref) if changed_decls is None else changed_decls
    if decls:
        return True
    if extra and _changed_under(repo, ref, list(extra)):
        return True
    return bool(_changed_under(repo, ref, list(RULE_PATHS)))


# meiva:note
# The baseline this checker would write for the checkout `base`: the commands a rule change runs on
# it, staged after each write because the checker reads tracked files only.
def regenerated(base: Checkout, ref: str, jobs: int | None) -> Baselines:
    # meiva:note
    # The declarations are written over the base's content, so an entry the change *adds* names a file
    # the base does not have, and the base reads it as a child that is not there (pull request 7625,
    # batch of 7600 and 7620). Pruning first drops exactly those entries: this baseline is the one this
    # checker would write for the base, and it would not write an export for a file that does not
    # exist. The refusal after it still stops a manifest that is malformed rather than merely ahead of
    # the base's content, which is what it is for.
    _, analysis = prune(base, jobs)
    _refuse_unreadable(analysis.diagnostics, ref)
    lowered = prune_debt(base, jobs, analysis=analysis)
    cached: Analysis | None = None if lowered else analysis
    declared, _ = declare(base, [""], jobs, analysis=cached)
    if declared:
        base.stage_all()
        cached = None
    _, created, _ = record(base, [""], jobs, analysis=cached)
    if created:
        base.stage_all()
    baselines = read_baselines(base.universe())
    if created or any(CYCLE_EDGE in table for table in baselines.values()):
        record(base, [""], jobs)
        base.stage_all()
        prune_debt(base, jobs)
        baselines = read_baselines(base.universe())
    return baselines


# meiva:note
# The device's own code, and only that: the rule code and the policy are the change's, everything else
# under `meizel/` is content and is measured as the reference left it. A wider copy would hide a
# violation the change adds there behind the change's own files.
def _sync_rule_files(repo: Checkout, ref: str, base: Checkout) -> None:
    byte_writes: dict[Path, bytes] = {}
    deletes: list[Path] = []
    for rel in _changed_under(repo, ref, list(RULE_PATHS)):
        dst = base.root / rel
        if repo.has_file(rel):
            byte_writes[dst] = repo.load_bytes(rel)
        elif base.has_file(rel):
            deletes.append(dst)
    if not byte_writes and not deletes:
        return
    base.apply_plan(EditPlan(byte_writes=byte_writes, deletes=tuple(deletes)))


# meiva:note
# The reference's content at the paths the change uses: a file git reports as renamed is moved before
# it is measured, so the device sees the change's paths and a rename is neither a rule change nor
# repayment. The bytes are the reference's own, never the change's.
def _apply_renames(base: Checkout, moved: Mapping[str, str]) -> None:
    byte_writes: dict[Path, bytes] = {}
    deletes: list[Path] = []
    dirs: list[Path] = []
    for old, new in sorted(moved.items()):
        if not base.has_file(old):
            continue
        target = base.root / new
        dirs.append(target.parent)
        byte_writes[target] = base.load_bytes(old)
        deletes.append(base.root / old)
    if not byte_writes:
        return
    base.apply_plan(EditPlan(byte_writes=byte_writes, deletes=tuple(deletes), dirs=tuple(dirs)))
    base.stage_all()


# meiva:note
# The head's manifests, minus their debt tables, over the base's content: what is measured is the
# head's device, and what it is measured in is the base's code. A manifest the head dropped is dropped
# from the base too, so deleting a declaration reads as a rule change as well.
def _apply_declarations(repo: Checkout, base: Checkout) -> None:
    staged = base.universe()
    head_paths = {path for path in repo.universe().files if path.rsplit("/", 1)[-1] == MANIFEST}
    base_paths = {path for path in staged.files if path.rsplit("/", 1)[-1] == MANIFEST}
    wanted = {path: set_debt(repo.load_text(path, default=""), {}) for path in sorted(head_paths)}
    writes = {
        base.root / path: text
        for path, text in wanted.items()
        if (base.load_text(path, default="") if path in staged.files else "") != text
    }
    deletes = tuple(base.root / path for path in sorted(base_paths - head_paths))
    if not writes and not deletes:
        return
    base.apply_plan(EditPlan(writes=writes, deletes=deletes, dirs=tuple(p.parent for p in writes)))


# meiva:note
# The change's content at a checkout of the ref: every path the diff touches takes the working tree's
# bytes, and a path the working tree no longer has is deleted there. This is the same replay as
# `_apply_declarations` with the pair swapped — the device stays where the ref left it and only what it
# measures moves — so `repaid` and `found` are read against one another.
def _apply_content(repo: Checkout, ref: str, checkout: Checkout, moved: Mapping[str, str]) -> None:
    byte_writes: dict[Path, bytes] = {}
    deletes: list[Path] = []
    dirs: list[Path] = []
    # meiva:note
    # The device stays at the ref and only what it measures moves. A manifest, the policy and every rule
    # file are the device, never the content: a manifest the change adds is not written here and one it
    # deletes is not removed, and a policy entry the change adds moves no debt in this column. A file the
    # change moved is measured where the ref had it, because the ref's declarations name that path — the
    # mirror of `_apply_renames`, which moves the ref's content to the change's paths for `found`.
    device = set(_changed_under(repo, ref, list(RULE_PATHS)))
    back = {new: old for old, new in moved.items()}
    for rel in _changed_under(repo, ref, []):
        if rel in device or rel.rsplit("/", 1)[-1] == MANIFEST:
            continue
        target = back.get(rel, rel)
        if repo.has_file(rel):
            path = checkout.root / target
            dirs.append(path.parent)
            byte_writes[path] = repo.load_bytes(rel)
        elif checkout.has_file(target):
            deletes.append(checkout.root / target)
    kept = [path for path in deletes if path not in byte_writes]
    if not byte_writes and not kept:
        return
    checkout.apply_plan(EditPlan(byte_writes=byte_writes, deletes=tuple(kept), dirs=tuple(dirs)))
    checkout.stage_all()


# meiva:note
# The declared symbol moves, applied to the base's own content: each import that names a moved symbol
# at its old home is pointed at the new one, so the change's device measures the same site at the same
# count instead of missing it. Only a declared move applies, only when the change really made it — the
# old file no longer defines the name and the new one does, and the diff touches both — and every
# rewrite is named in the report: a gate that quietly repaired the tree it measures could not be
# audited, and 2026-10-29's two queue failures were exactly a gate that never said which tree it read.
def _move_applies(repo: Checkout, head: Universe, ref: str, move: SymbolMove) -> bool:
    touched = set(_changed_under(repo, ref, [move.old, move.new]))
    if not {move.old, move.new} <= touched:
        return False
    if not (
        repo.has_file(move.new) and defines(head, move.new, repo.load_bytes(move.new), move.name)
    ):
        return False
    return not (
        repo.has_file(move.old) and defines(head, move.old, repo.load_bytes(move.old), move.name)
    )


def _retarget_one(base: Checkout, move: SymbolMove) -> list[str]:
    lines: list[str] = []
    for relative in sorted(base.universe().files):
        if not relative.endswith(".py") or move.name not in base.load_text(relative):
            continue
        updated, rewrites = retarget_imports(relative, base.load_text(relative), move)
        if not rewrites:
            continue
        base.apply_plan(EditPlan(writes={base.root / relative: updated}))
        lines.extend(
            f"base import rewritten: {relative}:{number} {old}.{move.name} -> {new}.{move.name} "
            "(Symbol-Move, declared in the pull request body)"
            for number, old, new in rewrites
        )
    return lines


def _apply_symbol_moves(
    repo: Checkout, ref: str, base: Checkout, moves: Sequence[SymbolMove]
) -> tuple[list[str], list[str]]:
    if not moves:
        return [], []
    head = repo.universe()
    rewritten: list[str] = []
    ignored: list[str] = []
    for move in moves:
        if not _move_applies(repo, head, ref, move):
            ignored.append(
                f"meizel: warning: `Symbol-Move: {move.old}::{move.name} -> {move.new}` does not "
                "describe this change (the name stays in its old file, the new file does not define "
                "it, or the diff does not touch both); ignored"
            )
            continue
        lines = _retarget_one(base, move)
        if not lines:
            ignored.append(
                f"meizel: warning: `Symbol-Move: {move.old}::{move.name} -> {move.new}` matches no "
                "import the base points at the old home; nothing to retarget"
            )
        rewritten.extend(lines)
    if rewritten:
        base.stage_all()
    return rewritten, ignored


# meiva:note
# Compare the tree `repo` reads with `ref`, given the per-code debt the base records.
def rule_diff(
    repo: Checkout,
    ref: str,
    base: Counter[str],
    moved: Mapping[str, str],
    jobs: int | None = None,
    symbols: Sequence[SymbolMove] = (),
) -> RuleDiff:
    with repo.checkout(ref) as checkout:
        prepare_policy_snapshot(checkout)
        # meiva:note
        # Normalizing the snapshot rewrites its policy, so the index is read after it: a universe
        # is a snapshot of the tree that was there when it was built.
        _refuse_unreadable(manifest_diagnostics(checkout.universe()), ref)
        _apply_renames(checkout, moved)
        _sync_rule_files(repo, ref, checkout)
        _apply_declarations(repo, checkout)
        checkout.stage_all()
        rewritten, ignored = _apply_symbol_moves(repo, ref, checkout, symbols)
        invalidate_policy_caches()
        found = regenerated(checkout, ref, jobs)
    with repo.checkout(ref) as checkout:
        prepare_policy_snapshot(checkout)
        _apply_content(repo, ref, checkout, moved)
        checkout.stage_all()
        invalidate_policy_caches()
        under_reference = regenerated(checkout, ref, jobs)
    return RuleDiff(
        base,
        Replays(under_change=found, under_reference=under_reference),
        recorded_when_merged(repo, ref),
        ref,
        rewritten,
        ignored,
    )
