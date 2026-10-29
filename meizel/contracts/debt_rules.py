# meiva:note
# `debt diff` for a change that edits the rules: the growth the rules cause, apart from the growth the content causes.
#
# A rule change makes the checker find debt that was always in the code, so the recorded total grows
# by design. That growth must not hide a content change that grows it too. Two measures:
#
# - Rules: what this checker finds in the BASE tree's content, minus what the base records.
# - Content: what the change records, minus what this checker finds in the base tree's content.
#
# "What this checker finds in the base tree" is the baseline it would write for the base: this checker
# (the code being judged) runs on a throwaway checkout of the base commit, declaring the legal edges and
# recording the rest, exactly as a rule change's own baseline commit does. The base's recorded debt is
# the old rule's result, since the base is green. Rules are reported and never judged. Content growth is
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
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import TypeGuard

from meizel.contracts.analysis import Analysis, analyze
from meizel.contracts.debt import TABLE_KEY, set_debt, total
from meizel.contracts.debt_edit import prune as prune_debt
from meizel.contracts.debt_edit import record
from meizel.contracts.debt_report import Baselines, manifest_texts_at, read_baselines
from meizel.contracts.declare import declare
from meizel.contracts.policy import POLICY, load_policy, policy_at
from meizel.contracts.policy import changes as policy_changes
from meizel.contracts.sync import prepare_policy_snapshot, prune
from meizel.graph.model import Code, MeizelError
from meizel.graph.universe import MANIFEST, EditPlan, GitRepo
from meizel.language.scan import invalidate_policy_caches

__all__ = ["RuleDiff", "declarations_changed", "rule_change", "rule_diff"]

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


class RuleDiff:
    """The two measures for one change, per code, and the keys that make the content measure fail."""

    def __init__(
        self, base: Counter[str], regenerated: Baselines, tree: Baselines, ref: str
    ) -> None:
        self.ref = ref
        self.base = base
        self.found = _totals(regenerated)
        self.tree = _totals(tree)
        self.excess = _excess(tree, regenerated)

    @property
    def content_grew(self) -> bool:
        return bool(self.excess)

    def measures(self) -> tuple[int, int]:
        return (
            sum(self.found.values()) - sum(self.base.values()),
            sum(self.tree.values()) - sum(self.found.values()),
        )

    def lines(self) -> list[str]:
        codes = sorted(set(self.base) | set(self.found) | set(self.tree))
        out = [
            f"{'code':<20}{self.ref[:12]:>14}{'new rules':>11}{'tree':>8}{'rules':>8}{'content':>9}"
        ]
        for code in codes:
            out.append(
                f"{code:<20}{self.base[code]:>14}{self.found[code]:>11}{self.tree[code]:>8}"
                f"{self.found[code] - self.base[code]:>+8}{self.tree[code] - self.found[code]:>+9}"
            )
        rules, content = self.measures()
        out.append(
            f"{'total':<20}{sum(self.base.values()):>14}{sum(self.found.values()):>11}"
            f"{sum(self.tree.values()):>8}{rules:>+8}{content:>+9}"
        )
        out.append(f"meizel: rules {rules:+d} / content {content:+d} against {self.ref[:12]}")
        return out

    def explanation(self) -> list[str]:
        if not self.excess:
            return [f"meizel: no content growth against {self.ref}: the rules account for the rest"]
        shown = self.excess[:_DEBT_ROWS]
        more = len(self.excess) - len(shown)
        return [
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
# Every (directory, code, key) the tree records beyond what the regenerated base records. The base is
# measured at the change's own paths (a moved file is moved before it is measured), so a rename is
# neither a rule change nor growth, and no key needs translating.
def _excess(tree: Baselines, found: Baselines) -> list[tuple[str, str, str, int, int]]:
    out: list[tuple[str, str, str, int, int]] = []
    for owner, table in sorted(tree.items()):
        for code, rows in table.items():
            for key, have in sorted(rows.items()):
                entry = found.get(owner, {}).get(code, {}).get(key)
                want = entry.count if entry is not None else 0
                if have.count > want:
                    out.append((owner or ".", code.value, key, have.count, want))
    return out


def _refuse_unreadable(analysis: Analysis, ref: str) -> None:
    unreadable = [d for d in analysis.diagnostics if d.code in _UNREADABLE]
    if unreadable:
        shown = "\n".join(d.render() for d in unreadable[:5])
        raise MeizelError(
            f"this checker cannot read the manifests of {ref}, so it cannot say what the new rules "
            f"find there:\n{shown}"
        )


# meiva:note
# The files that differ between `ref` and the working tree under the given git pathspecs; no
# pathspecs lists every changed file.
def _changed_under(repo: GitRepo, ref: str, pathspecs: list[str]) -> list[str]:
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
def declarations_changed(repo: GitRepo, ref: str) -> list[str]:
    before = manifest_texts_at(repo, ref)
    files = repo.universe().files
    after = {
        path: repo.load_text(path, default="")
        for path in sorted(files)
        if path.rsplit("/", 1)[-1] == MANIFEST
    }
    lines: list[str] = []
    for path in sorted(before.keys() | after.keys()):
        was = _declarations(before.get(path, ""))
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
def measurement_lines(repo: GitRepo, ref: str, extra: Collection[str] = ()) -> list[str]:
    universe = repo.universe()
    entries = [
        f"{POLICY} {line}"
        for line in policy_changes(policy_at(universe, ref), load_policy(universe)[0])
    ]
    if not entries and POLICY in _changed_under(repo, ref, [POLICY]):
        entries = [POLICY]
    return [
        *_changed_under(repo, ref, [*RULE_CODE, *extra]),
        *declarations_changed(repo, ref),
        *entries,
    ]


# meiva:note
# Whether the interval changes what is measured, not only what the recorded number says.
def rule_change(repo: GitRepo, ref: str, extra: Collection[str] = ()) -> bool:
    if declarations_changed(repo, ref):
        return True
    if extra and _changed_under(repo, ref, list(extra)):
        return True
    return bool(_changed_under(repo, ref, list(RULE_PATHS)))


# meiva:note
# The baseline this checker would write for the checkout `base`: the commands a rule change runs on
# it, staged after each write because the checker reads tracked files only.
def regenerated(base: GitRepo, ref: str, jobs: int | None) -> Baselines:
    # meiva:note
    # The declarations are written over the base's content, so an entry the change *adds* names a file
    # the base does not have, and the base reads it as a child that is not there (pull request 7625,
    # batch of 7600 and 7620). Pruning first drops exactly those entries: this baseline is the one this
    # checker would write for the base, and it would not write an export for a file that does not
    # exist. The refusal after it still stops a manifest that is malformed rather than merely ahead of
    # the base's content, which is what it is for.
    prune(base, jobs)
    _refuse_unreadable(analyze(base.universe(), jobs=jobs), ref)
    prune_debt(base, jobs)
    declare(base, [""], jobs)
    base.stage_all()  # the checker reads tracked files only, and these commands create manifests
    record(base, [""], jobs)
    base.stage_all()
    record(base, [""], jobs)
    base.stage_all()
    prune_debt(base, jobs)
    # meiva:note
    # The commands above wrote manifests and staged them: the index is rebuilt from the new tree.
    return read_baselines(base.universe())


# meiva:note
# The device's own code, and only that: the rule code and the policy are the change's, everything else
# under `meizel/` is content and is measured as the reference left it. A wider copy would hide a
# violation the change adds there behind the change's own files.
def _sync_rule_files(repo: GitRepo, ref: str, base: GitRepo) -> None:
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
    EditPlan(byte_writes=byte_writes, deletes=tuple(deletes)).apply()


# meiva:note
# The reference's content at the paths the change uses: a file git reports as renamed is moved before
# it is measured, so the device sees the change's paths and a rename is neither a rule change nor
# repayment. The bytes are the reference's own, never the change's.
def _apply_renames(base: GitRepo, moved: Mapping[str, str]) -> None:
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
    EditPlan(byte_writes=byte_writes, deletes=tuple(deletes), dirs=tuple(dirs)).apply()
    base.stage_all()


# meiva:note
# The head's manifests, minus their debt tables, over the base's content: what is measured is the
# head's device, and what it is measured in is the base's code. A manifest the head dropped is dropped
# from the base too, so deleting a declaration reads as a rule change as well.
def _apply_declarations(repo: GitRepo, base: GitRepo) -> None:
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
    EditPlan(writes=writes, deletes=deletes, dirs=tuple(p.parent for p in writes)).apply()
    base.stage_all()


# meiva:note
# Compare the tree `repo` reads with `ref`, given the per-code debt the base records.
def rule_diff(
    repo: GitRepo, ref: str, base: Counter[str], moved: Mapping[str, str], jobs: int | None = None
) -> RuleDiff:
    with repo.checkout(ref) as checkout:
        prepare_policy_snapshot(checkout)
        # meiva:note
        # Normalizing the snapshot rewrites its policy, so the index is read after it: a universe
        # is a snapshot of the tree that was there when it was built.
        _refuse_unreadable(analyze(checkout.universe(), jobs=jobs), ref)
        _apply_renames(checkout, moved)
        _sync_rule_files(repo, ref, checkout)
        _apply_declarations(repo, checkout)
        invalidate_policy_caches()
        checkout.stage_all()
        found = regenerated(checkout, ref, jobs)
    tree = repo.universe()
    return RuleDiff(base, found, read_baselines(tree), ref)
