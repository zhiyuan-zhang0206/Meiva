"""`debt diff` for a change that edits the rules: the growth the rules cause, apart from the growth the content causes.

A rule change makes the checker find debt that was always in the code, so the recorded total grows
by design. That growth must not hide a content change that grows it too. Two measures:

- Rule growth: what this checker finds in the BASE tree's content, minus what the base records.
- Content growth: what the change records, minus what this checker finds in the base tree's content.

"What this checker finds in the base tree" is the baseline it would write for the base: this checker
(the code being judged) runs on a throwaway checkout of the base commit, declaring the legal edges and
recording the rest, exactly as a rule change's own baseline commit does. The base's recorded debt is
the old rule's result, since the base is green. Rule growth is reported and never judged. Content
growth is judged: every key the change records must be one the regenerated base records, with no
greater count, so a declaration cannot hide new violations and a violation cannot ride along. A file
moved in the same change shows up as a new key and fails: move files in their own change.
"""

from __future__ import annotations

import tomllib
from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import TypeGuard

from meizel.allowances import RETIRED_AMBIENT_SECTIONS, Policy
from meizel.contracts.analysis import Analysis, analyze
from meizel.contracts.debt import total
from meizel.contracts.debt_edit import prune as prune_debt
from meizel.contracts.debt_edit import record
from meizel.contracts.debt_report import Baselines, changed_under, read_baselines
from meizel.contracts.declare import declare
from meizel.contracts.manifest import Manifest, Root, load_manifests, manifest_path
from meizel.contracts.manifest_text import set_scope
from meizel.contracts.sync import prepare_policy_snapshot, prune
from meizel.graph.model import AmbientKind, Code, MeizelError
from meizel.graph.universe import EditPlan, GitRepo, Universe, load_universe, parent_of
from meizel.language.scan import invalidate_policy_caches

__all__ = ["RuleDiff", "purity_changed", "rule_diff"]

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


class RuleDiff:
    """The two measures for one change, per code, and the keys that make the content measure fail."""

    def __init__(
        self,
        base: Counter[str],
        regenerated: Baselines,
        tree: Baselines,
        ref: str,
        unmasked: Mapping[tuple[str, str], frozenset[str]],
    ) -> None:
        self.ref = ref
        self.base = base
        self.found = _totals(regenerated)
        self.tree = _totals(tree)
        self.excess = _excess(tree, regenerated, unmasked)

    @property
    def content_grew(self) -> bool:
        return bool(self.excess)

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
        rules = sum(self.found.values()) - sum(self.base.values())
        content = sum(self.tree.values()) - sum(self.found.values())
        out.append(
            f"{'total':<20}{sum(self.base.values()):>14}{sum(self.found.values()):>11}"
            f"{sum(self.tree.values()):>8}{rules:>+8}{content:>+9}"
        )
        return out

    def explanation(self) -> list[str]:
        if not self.excess:
            return [f"meizel: no content growth against {self.ref}: the rules account for the rest"]
        shown = self.excess[:_DEBT_ROWS]
        more = len(self.excess) - len(shown)
        return [
            f"meizel: the change records debt the new rules do not find in {self.ref[:12]}'s content:",
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
# Every (directory, code, key) the tree records beyond what the regenerated base records, except the
# reads a root declaration of the base was hiding: a file whose `[roots]` entry the change removes
# shows its reads, and recording them is the honest way to remove the entry (the base's roots are
# kept when it is regenerated, so it never finds them). Only a kind the change took from the entry
# is let through, for the file of that entry.
def _excess(
    tree: Baselines, found: Baselines, unmasked: Mapping[tuple[str, str], frozenset[str]]
) -> list[tuple[str, str, str, int, int]]:
    out: list[tuple[str, str, str, int, int]] = []
    for owner, table in sorted(tree.items()):
        for code, rows in table.items():
            for key, have in sorted(rows.items()):
                want = found.get(owner, {}).get(code, {}).get(key, 0)
                file, _, what = key.partition("#")
                kind = what.split(" ", 1)[0]
                if have > want and kind not in unmasked.get((owner, file), frozenset()):
                    out.append((owner or ".", code.value, key, have, want))
    return out


# meiva:note
# The kinds the change takes away from a `[roots]` entry of the base, per (directory, file).
def unmasked_reads(
    base: Mapping[str, Manifest], tree: Mapping[str, Manifest]
) -> dict[tuple[str, str], frozenset[str]]:
    freed: dict[tuple[str, str], frozenset[str]] = {}
    for directory, manifest in base.items():
        now = tree.get(directory)
        for name, root in manifest.roots.items():
            kept = now.roots[name].kinds if now is not None and name in now.roots else ()
            gone = frozenset(kind.value for kind in root.kinds) - frozenset(k.value for k in kept)
            if gone:
                freed[(directory, name)] = gone
    return freed


def _refuse_unreadable(analysis: Analysis, ref: str) -> None:
    unreadable = [d for d in analysis.diagnostics if d.code in _UNREADABLE]
    if unreadable:
        shown = "\n".join(d.render() for d in unreadable[:5])
        raise MeizelError(
            f"this checker cannot read the manifests of {ref}, so it cannot say what the new rules "
            f"find there:\n{shown}"
        )


def regenerated(base_root: Path, ref: str, jobs: int | None) -> Baselines:
    """The baseline this checker would write for the checkout at `base_root`."""
    _refuse_unreadable(analyze(base_root, jobs=jobs), ref)
    repo = GitRepo(base_root)
    prune(base_root, jobs)
    prune_debt(base_root, jobs)
    declare(base_root, [""], jobs)
    repo.stage_all()  # the checker reads tracked files only, and these commands create manifests
    record(base_root, [""], jobs)
    repo.stage_all()
    record(base_root, [""], jobs)
    repo.stage_all()
    prune_debt(base_root, jobs)
    return read_baselines(load_universe(base_root))


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_table(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(k, str) for k in value)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_str_list(value: object) -> TypeGuard[list[str]]:
    return _is_object_list(value) and all(isinstance(item, str) for item in value)


def _strict_to_be_targets(text: str) -> set[str]:
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return set()
    purity = doc.get("purity")
    if purity == "strict-to-be":
        return {"."}
    if _is_table(purity):
        return {k for k, v in purity.items() if v == "strict-to-be"}
    return set()


# meiva:note
# Reads a tracked manifest blob from a git ref using a fixed git show argv without invoking a shell.
def _base_text(root: Path, ref: str, path: str) -> str:
    return GitRepo(root).show_optional_text(ref, path) or ""


def purity_changed(root: Path, ref: str) -> bool:
    changed = set(changed_under(root, ref, [":(glob)**/meizel.toml", "meizel.toml"]))
    if not changed:
        return False
    manifests, _ = load_manifests(load_universe(root))
    for d, m in manifests.items():
        rel = manifest_path(d)
        if rel not in changed:
            continue
        now = {k for k, v in m.purity.items() if v == "strict-to-be"}
        if m.purity_default == "strict-to-be":
            now.add(".")
        if now - _strict_to_be_targets(_base_text(root, ref, rel)):
            return True
    return False


# meiva:note
# Historical comparison migrates only the base's own registry facts. A head root permission
# never enters this map: adding or widening one remains a real manifest change and debt decrease.
def _legacy_sources(universe: Universe, policy: Policy) -> dict[str, set[AmbientKind]]:
    sources: dict[str, set[AmbientKind]] = {}
    sections = {
        "config-modules": AmbientKind.SETTINGS,
        "db-modules": AmbientKind.DB,
        "db-handles": AmbientKind.DB,
        "bus-handles": AmbientKind.BUS,
        "endpoint-handles": AmbientKind.ENDPOINTS,
    }
    for section, kind in sections.items():
        for name in policy.named.get(("ambient-state", section), ()):
            module = universe.resolve_module(name.partition(":")[0])
            if module is not None and module.file is not None:
                sources.setdefault(module.file, set()).add(kind)
    return sources


def _historical_root_modules(
    path: str, name: str, value: object
) -> tuple[AmbientKind, tuple[str, ...]]:
    try:
        kind = AmbientKind(name)
    except ValueError as error:
        raise MeizelError(f"{path}: unknown historical root kind {name!r}") from error
    held = {AmbientKind.SETTINGS, AmbientKind.DB, AmbientKind.BUS, AmbientKind.ENDPOINTS}
    if kind not in held:
        raise MeizelError(f"{path}: unknown historical root kind {name!r}")
    if not _is_str_list(value) or (not value and kind is AmbientKind.SETTINGS):
        raise MeizelError(f"{path}: {name} must be a list of modules")
    return kind, tuple(value)


def _legacy_roots(
    universe: Universe, policy: Policy, registries: dict[str, str]
) -> tuple[dict[str, set[AmbientKind]], dict[str, str]]:
    roots: dict[str, set[AmbientKind]] = {}
    reasons = policy.reasons("no-os-environ", "files")
    for path in reasons:
        if path in universe.files:
            roots.setdefault(path, set()).add(AmbientKind.ENVIRON)
    for path, text in sorted(registries.items()):
        try:
            document = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise MeizelError(f"{path}: invalid historical root registry: {error}") from error
        for name, value in document.items():
            kind, modules = _historical_root_modules(path, name, value)
            for item in modules:
                target = f"{parent_of(path)}/{item}".lstrip("/")
                if target in universe.files:
                    roots.setdefault(target, set()).add(kind)
    return roots, reasons


def _merge_kinds(
    directory: str,
    declared: dict[str, tuple[AmbientKind, ...]],
    additions: dict[str, set[AmbientKind]],
) -> dict[str, tuple[AmbientKind, ...]]:
    merged = {name: set(kinds) for name, kinds in declared.items()}
    for path, kinds in additions.items():
        if parent_of(path) == directory:
            merged.setdefault(Path(path).name, set()).update(kinds)
    return {name: tuple(sorted(kinds)) for name, kinds in merged.items()}


def _ambient_manifest(
    manifest: Manifest,
    sources: dict[str, set[AmbientKind]],
    roots: dict[str, set[AmbientKind]],
    reasons: dict[str, str],
) -> Manifest:
    allowed = _merge_kinds(
        manifest.directory, {n: r.kinds for n, r in manifest.roots.items()}, roots
    )
    permissions = {
        name: Root(
            kinds,
            manifest.roots.get(name, Root(())).why
            or reasons.get(f"{manifest.directory}/{name}".lstrip("/"), ""),
        )
        for name, kinds in allowed.items()
    }
    return replace(
        manifest, state=_merge_kinds(manifest.directory, manifest.state, sources), roots=permissions
    )


def _ambient_snapshot(
    universe: Universe, policy: Policy, manifests: dict[str, Manifest], registries: dict[str, str]
) -> dict[str, Manifest]:
    if not any(("ambient-state", key) in policy.named for key in RETIRED_AMBIENT_SECTIONS):
        return manifests
    prepared = dict(manifests)
    sources = _legacy_sources(universe, policy)
    roots, reasons = _legacy_roots(universe, policy, registries)
    directories = {parent_of(path) for path in sources.keys() | roots.keys()}
    for directory in sorted(directories):
        manifest = manifests.get(directory, Manifest(directory))
        prepared[directory] = _ambient_manifest(manifest, sources, roots, reasons)
    return prepared


def _sync_rule_files(root: Path, ref: str, base_root: Path) -> set[str]:
    src_repo = GitRepo(root)
    dst_repo = GitRepo(base_root)
    byte_writes: dict[Path, bytes] = {}
    deletes: list[Path] = []
    copied: set[str] = set()
    for rel in changed_under(root, ref, [":(glob)meizel/**", "meizel.policy.toml"]):
        dst = base_root / rel
        if src_repo.has_file(rel):
            byte_writes[dst] = src_repo.load_bytes(rel)
            if Path(rel).name == "meizel.toml":
                copied.add(rel)
        elif dst_repo.has_file(rel):
            deletes.append(dst)
    EditPlan(byte_writes=byte_writes, deletes=tuple(deletes)).apply()
    return copied


def _scope_text(
    text: str, manifest: Manifest | None, baseline: Manifest, known: frozenset[str]
) -> str:
    if manifest is not None and (manifest.purity_default is not None or manifest.purity):
        children = {
            k: v
            for k, v in manifest.purity.items()
            if (f"{manifest.directory}/{k}" if manifest.directory else k) in known
        }
        levels = (
            {".": manifest.purity_default} if manifest.purity_default is not None else {}
        ) | children
        text = set_scope(text, "purity", levels, manifest.purity_why)
    text = set_scope(text, "state", baseline.state, {})
    return set_scope(
        text,
        "roots",
        {n: r.kinds for n, r in baseline.roots.items()},
        {n: r.why for n, r in baseline.roots.items()},
    )


# meiva:note
# Rule code and purity scope use the head. Ambient permissions stay the base's own prepared
# facts, including when a checker manifest is copied with the rule code. All scope edits use
# this existing manifest effect loop rather than a second migration writer.
def _sync_purity_scope(
    root: Path, ref: str, base_root: Path, original: dict[str, Manifest]
) -> None:
    tree_manifests, _ = load_manifests(load_universe(root))
    copied = _sync_rule_files(root, ref, base_root)
    invalidate_policy_caches()
    base_repo = GitRepo(base_root)
    base_repo.stage_all()
    base_universe = load_universe(base_root)
    known = base_universe.files | base_universe.dirs
    writes: dict[Path, str] = {}
    for directory in sorted(tree_manifests.keys() | original.keys()):
        manifest = tree_manifests.get(directory)
        baseline = original.get(directory, Manifest(directory))
        purity = manifest is not None and (manifest.purity_default is not None or manifest.purity)
        ambient = bool(baseline.state or baseline.roots or manifest_path(directory) in copied)
        if not purity and not ambient:
            continue
        if directory and directory not in base_universe.dirs:
            continue
        rel = manifest_path(directory)
        old_text = base_repo.load_text(rel, default="")
        new_text = _scope_text(old_text, manifest, baseline, known)
        if new_text != old_text:
            writes[base_root / rel] = new_text
    EditPlan(writes=writes).apply()
    base_repo.stage_all()


def rule_diff(root: Path, ref: str, base: Counter[str], jobs: int | None = None) -> RuleDiff:
    """Compare the tree at `root` with `ref`, given the per-code debt the base records (`base`)."""
    with GitRepo(root).checkout(ref) as checkout:
        historical = prepare_policy_snapshot(checkout)
        _refuse_unreadable(analyze(checkout, jobs=jobs), ref)
        universe = load_universe(checkout)
        manifests, _ = load_manifests(universe)
        registries = {
            path: _base_text(checkout, "HEAD", path)
            for path in universe.files
            if Path(path).name == "ambient_roots.toml"
        }
        baseline = _ambient_snapshot(universe, historical, manifests, registries)
        _sync_purity_scope(root, ref, checkout, baseline)
        found = regenerated(checkout, ref, jobs)
    tree_universe = load_universe(root)
    freed = unmasked_reads(baseline, load_manifests(tree_universe)[0])
    return RuleDiff(base, found, read_baselines(tree_universe), ref, freed)
