"""The rule: project a fact edge onto its lowest common directory and judge it."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import NamedTuple

from meizel.contracts.debt import PURITY_CODES, file_key, source_of
from meizel.contracts.manifest import Manifest, effective_purity, manifest_path, within
from meizel.contracts.policy import Policy
from meizel.graph.graphs import reachable
from meizel.graph.model import Code, DebtKey, Diagnostic, Fact, FactKind
from meizel.graph.universe import Universe, is_private_module
from meizel.language import (
    ALLOWED_STDLIB_MODULES,
    DEFAULT_IMPURE_CALLS,
    IMPURE_DOTTED_STDLIB_MODULES,
    IMPURE_STDLIB_MODULES,
)
from meizel.language.fixtures import (
    DEFAULT_IMPURE_FIXTURES,
    FixtureFunction,
    FixtureIndex,
    is_test_file,
)
from meizel.language.scan import FileScan

__all__ = [
    "Projection",
    "is_private",
    "judge_facts",
    "paid_strict_to_be",
    "project",
    "purity_diagnostics",
    "stale_exports",
]


class Projection(NamedTuple):
    """A fact edge seen from `lca`, the deepest directory containing both ends."""

    fact: Fact
    lca: str
    cf: str
    ct: str
    steps: tuple[tuple[str, str], ...]

    @property
    def local_edge(self) -> tuple[str, str, str]:
        return (self.lca, self.cf, self.ct)

    @property
    def exempt(self) -> bool:
        """An ancestor conftest is a lexical scope of the tests below it."""
        fact = self.fact
        if fact.kind is not FactKind.FIXTURE or not fact.dst.endswith("conftest.py"):
            return False
        owner = fact.dst.rpartition("/")[0]
        return not owner or fact.src.startswith(f"{owner}/")

    # meiva:note
    # The fact's source and target, relative to the directory that judges it.
    def relative(self) -> tuple[str, str]:
        cut = len(self.lca) + 1 if self.lca else 0
        return self.fact.src[cut:], self.fact.dst[cut:]

    # meiva:note
    # The debt a violation of this projection would be recorded as, owned by its `lca`.
    def debt_key(self, name: str | None = None) -> DebtKey:
        src, dst = self.relative()
        key = f"{src} -> {dst}"
        return DebtKey(self.lca, f"{key}#{name}" if name else key)

    # meiva:note
    # A fact whose local edge is a recorded back-edge: it closes a cycle in `lca`.
    def cycle_edge(self) -> Diagnostic:
        fact = self.fact
        return Diagnostic(
            *_site(fact),
            Code.CYCLE_EDGE,
            f"{fact.edge()} ({fact.detail}) closes a cycle: {self.cf} -> {self.ct} is a back-edge "
            f"of {_where(self.lca)}",
            (
                "break the cycle: use a public door, inject the collaborator, or move the code so "
                "the dependency points the other way",
            ),
            self.debt_key(),
        )


def _project_pair(src: str, dst: str) -> tuple[str, str, str, tuple[tuple[str, str], ...]] | None:
    if src == dst:
        return None
    src_parts = src.split("/")
    dst_parts = dst.split("/")
    depth = 0
    while (
        depth < len(src_parts) - 1
        and depth < len(dst_parts) - 1
        and src_parts[depth] == dst_parts[depth]
    ):
        depth += 1
    if src_parts[depth] == dst_parts[depth]:
        return None
    steps = tuple(
        ("/".join(dst_parts[:index]), dst_parts[index])
        for index in range(depth + 1, len(dst_parts))
    )
    return "/".join(src_parts[:depth]), src_parts[depth], dst_parts[depth], steps


def project(fact: Fact) -> Projection | None:
    """None when the edge stays inside one node (a file, or a directory it loads from)."""
    geometry = _project_pair(fact.src, fact.dst)
    return None if geometry is None else Projection(fact, *geometry)


def is_private(name: str) -> bool:
    """A leading underscore marks a name private to its defining file; dunders are protocol."""
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _site(fact: Fact) -> tuple[str, int]:
    """Where to report a fact: its source line, or the manifest line that declares a load."""
    if fact.kind is FactKind.LOAD:
        return manifest_path(fact.src.rpartition("/")[0]), fact.line
    return fact.src, fact.line


def _where(directory: str) -> str:
    return directory or "."


def _undeclared(proj: Projection, manifests: dict[str, Manifest]) -> Diagnostic:
    fact, lca = proj.fact, proj.lca
    graph = manifests[lca].graph() if lca in manifests else {}
    hints = [
        f"L = {_where(lca)}; local edge {proj.cf} -> {proj.ct}",
        f'to declare it, add to {manifest_path(lca)}:  [uses]  "{proj.cf}" = [..., "{proj.ct}"]',
    ]
    narrowed = manifests[lca].narrowed(proj.cf, proj.ct) if lca in manifests else []
    if narrowed:
        listing = ", ".join(f"{s} -> {d}" for s, d in narrowed)
        hints.append(
            f"{proj.cf} -> {proj.ct} is declared only for narrowed endpoints ({listing}); "
            "this fact is outside them: fix the code, or ask for the narrowing to be widened"
        )
    if proj.cf in reachable(graph, proj.ct):
        hints.append(
            f"declaring it closes a cycle ({proj.ct} already reaches {proj.cf}); "
            "use a public door or inject the collaborator instead"
        )
    return Diagnostic(
        *_site(fact),
        Code.UNDECLARED_EDGE,
        f"{fact.edge()} ({fact.detail}) is not declared",
        tuple(hints),
        proj.debt_key(),
    )


def _blocked(proj: Projection, manifests: dict[str, Manifest]) -> Diagnostic | None:
    fact = proj.fact
    last = len(proj.steps) - 1
    for position, (directory, child) in enumerate(proj.steps):
        if child in Universe.DOOR_FILES and position == last:
            continue
        if is_private_module(child):
            hint = f"{child!r} is private to {_where(directory)} and cannot be exported; import its door `{_where(directory)}/__init__.py` or a sibling that is exported"
            return Diagnostic(
                *_site(fact),
                Code.PRIVATE_MODULE,
                f"{fact.edge()} ({fact.detail}) goes through the underscore child {child!r}",
                (hint,),
                proj.debt_key(),
            )
        if directory not in manifests or child not in manifests[directory].export_set:
            hint = f'add to {manifest_path(directory)}:  exports = [..., "{child}"]; or use the door `{_where(directory)}/__init__.py`'
            return Diagnostic(
                *_site(fact),
                Code.NOT_EXPORTED,
                f"{fact.edge()} ({fact.detail}) enters {_where(directory)} through {child!r}, which is not exported",
                (hint,),
                proj.debt_key(),
            )
    return None


def _private_name_diagnostics(proj: Projection) -> list[Diagnostic]:
    fact = proj.fact
    return [
        Diagnostic(
            *_site(fact),
            Code.PRIVATE_NAME,
            f"{fact.edge()} uses {name!r}, which is private to {fact.dst}",
            (
                "underscore names belong to the file that defines them, tests included; use a public name",
            ),
            proj.debt_key(name),
        )
        for name in fact.names
        if is_private(name)
    ]


# meiva:note
# Every way one fact edge violates the three conditions of the rule.
def _judge(proj: Projection, manifests: dict[str, Manifest]) -> list[Diagnostic]:
    fact = proj.fact
    found: list[Diagnostic] = []
    if not proj.exempt:
        manifest = manifests.get(proj.lca)
        if manifest is None or not manifest.covering(*proj.relative()):
            found.append(_undeclared(proj, manifests))
        blocked = _blocked(proj, manifests)
        if blocked is not None:
            found.append(blocked)
    manifest = manifests.get(proj.lca)
    if not proj.exempt and manifest is not None and (proj.cf, proj.ct) in manifest.back:
        found.append(proj.cycle_edge())
    if fact.names:
        found.extend(_private_name_diagnostics(proj))
    return found


@dataclass(frozen=True, slots=True)
class _CleanEdge:
    lca: str
    cf: str
    ct: str
    steps: tuple[tuple[str, str], ...]
    covered: tuple[tuple[str, str], ...]


def _steps_clean(steps: tuple[tuple[str, str], ...], manifests: dict[str, Manifest]) -> bool:
    last = len(steps) - 1
    for position, (directory, child) in enumerate(steps):
        if child in Universe.DOOR_FILES and position == last:
            continue
        if is_private_module(child):
            return False
        if directory not in manifests or child not in manifests[directory].export_set:
            return False
    return True


type _PairEntry = _CleanEdge | tuple[str, str, str, tuple[tuple[str, str], ...]] | None


def _classify_pair(src: str, dst: str, manifests: dict[str, Manifest]) -> _PairEntry:
    geometry = _project_pair(src, dst)
    if geometry is None:
        return None
    lca, cf, ct, steps = geometry
    manifest = manifests.get(lca)
    if dst.endswith("conftest.py") or manifest is None or (cf, ct) in manifest.back:
        return geometry
    cut = len(lca) + 1 if lca else 0
    covered = tuple(manifest.covering(src[cut:], dst[cut:]))
    if not covered or not _steps_clean(steps, manifests):
        return geometry
    return _CleanEdge(lca, cf, ct, steps, covered)


def judge_facts(
    facts: list[Fact], manifests: dict[str, Manifest]
) -> tuple[list[Projection], list[Diagnostic], dict[tuple[str, str, str], list[Fact]]]:
    pairs: dict[tuple[str, str], _PairEntry] = {}
    projections: list[Projection] = []
    found: list[Diagnostic] = []
    needed: dict[tuple[str, str, str], list[Fact]] = defaultdict(list)
    for fact in facts:
        pair = (fact.src, fact.dst)
        entry = pairs[pair] if pair in pairs else _classify_pair(fact.src, fact.dst, manifests)
        if pair not in pairs:
            pairs[pair] = entry
        if entry is None:
            continue
        if isinstance(entry, _CleanEdge):
            proj = Projection(fact, entry.lca, entry.cf, entry.ct, entry.steps)
            projections.append(proj)
            if fact.names:
                found.extend(_private_name_diagnostics(proj))
            for source, target in entry.covered:
                needed[(entry.lca, source, target)].append(fact)
            continue
        proj = Projection(fact, *entry)
        projections.append(proj)
        found.extend(_judge(proj, manifests))
        manifest = manifests.get(proj.lca)
        if not proj.exempt and manifest is not None:
            for source, target in manifest.covering(*proj.relative()):
                needed[(proj.lca, source, target)].append(fact)
    return projections, found, dict(needed)


def _purity_debt(owner: str, path: str, key: str, mode: str) -> DebtKey | None:
    return None if mode == "strict" else DebtKey(owner, file_key(owner, path, key))


def _module_purity_diagnostics(
    scan: FileScan,
    owner: str,
    mode: str,
    manifests: dict[str, Manifest],
) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    seen_edges: set[tuple[int, str]] = set()
    for fact in scan.facts:
        if fact.kind not in {FactKind.IMPORT, FactKind.ATTRIBUTE} or fact.dst == scan.path:
            continue
        if (
            effective_purity(fact.dst, manifests, include_tests=is_test_file(scan.path)) == "none"
            and (fact.line, fact.dst) not in seen_edges
        ):
            seen_edges.add((fact.line, fact.dst))
            found.append(
                Diagnostic(
                    scan.path,
                    fact.line,
                    Code.IMPURE_EDGE,
                    f"{fact.edge()} ({fact.detail}): {mode} module depends on impure module {fact.dst}",
                    (
                        "mark the dependency `strict` or `strict-to-be`, or move the impure dependency to an adapter outside the strict core",
                    ),
                    _purity_debt(owner, scan.path, fact.dst, mode),
                )
            )
    for item in scan.purity:
        if item.code is Code.NESTED_PATCH_TARGET and is_test_file(scan.path):
            continue
        found.append(
            Diagnostic(
                scan.path,
                item.line,
                item.code,
                item.message,
                (item.hint,),
                _purity_debt(owner, scan.path, item.key, mode),
            )
        )
    return found


def _test_purity_mode(scan: FileScan, manifests: dict[str, Manifest]) -> str:
    own_mode = effective_purity(scan.path, manifests, include_tests=True)
    targets = {
        f.dst
        for f in scan.facts
        if f.kind in {FactKind.IMPORT, FactKind.ATTRIBUTE, FactKind.PATCH} and f.dst != scan.path
    }
    if not targets:
        return own_mode
    target_modes = {effective_purity(dst, manifests) for dst in targets}
    if "none" in target_modes:
        return "none"
    return (
        "strict-to-be" if own_mode == "strict-to-be" or "strict-to-be" in target_modes else "strict"
    )


def _build_fixture_index(
    scans: Mapping[str, FileScan], universe: Universe | None = None
) -> FixtureIndex:
    defined = {
        p: s.fixtures.defs | s.imported
        for p, s in scans.items()
        if s.fixtures.defs or p.endswith("conftest.py") or s.fixtures.plugins
    }
    plugins = {
        p: [n for n, _ in s.fixtures.plugins] for p, s in scans.items() if s.fixtures.plugins
    }
    return FixtureIndex(universe, defined, plugins)


def _is_module_impure(
    mod_name: str,
    index: FixtureIndex,
    manifests: Mapping[str, Manifest],
    pure_third_party: frozenset[str],
) -> bool:
    top = mod_name.split(".", 1)[0]
    if index.universe is not None and (
        index.universe.is_first_party_name(top)
        or index.universe.resolve_module(mod_name) is not None
    ):
        mod = index.universe.resolve_module(mod_name)
        return effective_purity(mod.file, manifests) == "none" if mod and mod.file else False
    parts = mod_name.split(".")
    for i in range(len(parts), 0, -1):
        if ".".join(parts[:i]) in IMPURE_DOTTED_STDLIB_MODULES:
            return True
    if top in IMPURE_STDLIB_MODULES:
        return True
    return False if top in ALLOWED_STDLIB_MODULES else top not in pure_third_party


def _has_impure_fact(
    fn: FixtureFunction, scan: FileScan, manifests: Mapping[str, Manifest]
) -> bool:
    for fact in scan.facts:
        if fact.kind == FactKind.IMPORT:
            target = fact.dst
            if not is_test_file(target) and effective_purity(target, manifests) == "none":
                base = target.rpartition("/")[2].removesuffix(".py")
                if base in fn.names_loaded or any(sym in fn.names_loaded for sym in fact.names):
                    return True
    return False


def _is_function_direct_impure(
    fn: FixtureFunction,
    scan: FileScan,
    index: FixtureIndex,
    manifests: Mapping[str, Manifest],
    pure_third_party: frozenset[str],
) -> bool:
    if fn.is_async and fn.registered is not None:
        return True
    if any(c in DEFAULT_IMPURE_CALLS for c in fn.callees):
        return True
    if any(_is_module_impure(m, index, manifests, pure_third_party) for m in fn.imported_modules):
        return True
    if any(fn.line <= a.line <= fn.end_line for a in scan.ambient):
        return True
    return _has_impure_fact(fn, scan, manifests)


def _infer_helper_taint(
    scan: FileScan,
    index: FixtureIndex,
    manifests: Mapping[str, Manifest],
    pure_third_party: frozenset[str],
) -> set[str]:
    impure = {
        h.name
        for h in scan.fixtures.helpers
        if _is_function_direct_impure(h, scan, index, manifests, pure_third_party)
    }
    changed = True
    while changed:
        changed = False
        for h in scan.fixtures.helpers:
            if h.name not in impure and any(c in impure for c in h.local_calls):
                impure.add(h.name)
                changed = True
    return impure


def _collect_file_fixtures(
    path: str,
    scan: FileScan,
    index: FixtureIndex,
    manifests: Mapping[str, Manifest],
    pure_third_party: frozenset[str],
) -> tuple[dict[tuple[str, str], FixtureFunction], set[tuple[str, str]]]:
    file_fixtures: dict[tuple[str, str], FixtureFunction] = {}
    direct: set[tuple[str, str]] = set()
    helper_impure = _infer_helper_taint(scan, index, manifests, pure_third_party)
    for fn in scan.fixtures.functions:
        if fn.registered is not None:
            key = (path, fn.registered)
            file_fixtures[key] = fn
            if _is_function_direct_impure(fn, scan, index, manifests, pure_third_party) or any(
                c in helper_impure for c in fn.local_calls
            ):
                direct.add(key)
    return file_fixtures, direct


def _propagate_fixture_taint(
    all_fixtures: Mapping[tuple[str, str], FixtureFunction],
    impure: set[tuple[str, str]],
    index: FixtureIndex,
    builtins_impure: frozenset[str],
) -> None:
    changed = True
    while changed:
        changed = False
        for (path, name), fn in all_fixtures.items():
            if (path, name) in impure:
                continue
            if any(
                r in builtins_impure or any((c, r) in impure for c in index.candidates(r, path)[0])
                for r in fn.requests
            ):
                impure.add((path, name))
                changed = True


def _infer_impure_fixtures(
    index: FixtureIndex,
    scans: Mapping[str, FileScan],
    manifests: Mapping[str, Manifest],
    pure_third_party: frozenset[str],
    external_impure_fixtures: frozenset[str] = frozenset(),
) -> frozenset[tuple[str, str]]:
    all_fixtures: dict[tuple[str, str], FixtureFunction] = {}
    direct_impure: set[tuple[str, str]] = set()
    for path, scan in scans.items():
        if is_test_file(path) or "fixture" in path or path.endswith("conftest.py"):
            f, d = _collect_file_fixtures(path, scan, index, manifests, pure_third_party)
            all_fixtures.update(f)
            direct_impure.update(d)
    builtins = DEFAULT_IMPURE_FIXTURES | external_impure_fixtures
    impure = set(direct_impure)
    _propagate_fixture_taint(all_fixtures, impure, index, builtins)
    return frozenset(impure)


def _is_impure_fixture_request(
    name: str,
    test_path: str,
    impure_fixtures: frozenset[tuple[str, str]],
    index: FixtureIndex,
    external_impure: frozenset[str] = frozenset(),
) -> bool:
    if name in DEFAULT_IMPURE_FIXTURES or name in external_impure:
        return True
    return any((c, name) in impure_fixtures for c in index.candidates(name, test_path)[0])


def _test_purity_diagnostics(
    scan: FileScan,
    owner: str,
    manifests: dict[str, Manifest],
    impure_fixtures: frozenset[tuple[str, str]],
    index: FixtureIndex | None = None,
    external_impure: frozenset[str] = frozenset(),
) -> list[Diagnostic]:
    test_mode = _test_purity_mode(scan, manifests)
    if test_mode == "none":
        return []
    found = [
        Diagnostic(
            scan.path,
            req.line,
            Code.IMPURE_TEST_FIXTURE,
            f"test of strict modules requests impure fixture `{req.name}`",
            (
                "pass pure values or fakes directly instead of requesting heavy I/O fixtures or monkeypatch",
            ),
            _purity_debt(owner, scan.path, f"fixture {req.name}", test_mode),
        )
        for req in scan.fixtures.requests
        if (
            _is_impure_fixture_request(
                req.name, scan.path, impure_fixtures, index, external_impure=external_impure
            )
            if index is not None
            else req.name in external_impure
        )
    ]
    found.extend(
        Diagnostic(
            scan.path,
            item.line,
            item.code,
            item.message,
            (item.hint,),
            _purity_debt(owner, scan.path, item.key, test_mode),
        )
        for item in scan.purity
        if item.code is Code.NESTED_PATCH_TARGET
    )
    return found


def _file_purity_diagnostics(
    scan: FileScan,
    manifests: dict[str, Manifest],
    impure_fixtures: frozenset[tuple[str, str]],
    index: FixtureIndex | None = None,
    external_impure: frozenset[str] = frozenset(),
) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    owner = scan.path.rpartition("/")[0]
    mode = effective_purity(scan.path, manifests)
    if mode in {"strict", "strict-to-be"}:
        found.extend(_module_purity_diagnostics(scan, owner, mode, manifests))
    if is_test_file(scan.path):
        found.extend(
            _test_purity_diagnostics(
                scan, owner, manifests, impure_fixtures, index, external_impure
            )
        )
    return found


def _policy_limit(policy: Policy, section: str, default: int) -> int:
    raw = policy.names("purity", section)
    return int(raw[0]) if raw and raw[0].isdigit() else default


def _wide_fanout_diagnostics(manifests: dict[str, Manifest], policy: Policy) -> list[Diagnostic]:
    max_uses = _policy_limit(policy, "max-uses", 8)
    max_exports = _policy_limit(policy, "max-exports", 4)
    found: list[Diagnostic] = []
    for directory, manifest in sorted(manifests.items()):
        if (
            "/" in directory
            and manifest.purity_default in {"strict", "strict-to-be"}
            and len(manifest.exports) > max_exports
        ):
            found.append(
                Diagnostic(
                    manifest.path,
                    manifest.line_of("exports"),
                    Code.WIDE_EXPORTS,
                    f"exports: {_where(directory)} exports {len(manifest.exports)} children (max {max_exports} in {manifest.purity_default} mode)",
                    (
                        "keep the package door narrow by re-exporting through `__init__.py` or splitting subpackages",
                    ),
                    _purity_debt(directory, manifest.path, "exports", manifest.purity_default),
                )
            )
        for child, targets in sorted(manifest.uses.items()):
            child_path = f"{directory}/{child}" if directory else child
            mode = effective_purity(child_path, manifests)
            if mode in {"strict", "strict-to-be"} and len(targets) > max_uses:
                found.append(
                    Diagnostic(
                        manifest.path,
                        manifest.line_of(child),
                        Code.WIDE_FANOUT,
                        f"[uses] {child!r} depends on {len(targets)} sibling targets (max {max_uses} in {mode} mode)",
                        (
                            "split the module's responsibilities or group lower-level collaborators behind a cohesive subpackage door",
                        ),
                        _purity_debt(directory, child_path, "uses", mode),
                    )
                )
    return found


def _domain_subset_diagnostics(
    all_scans: Iterable[FileScan],
    judged_scans: Iterable[FileScan],
    manifests: dict[str, Manifest],
) -> list[Diagnostic]:
    by_subset: dict[tuple[str, tuple[str, ...]], set[str]] = defaultdict(set)
    for scan in all_scans:
        if not is_test_file(scan.path):
            for use in scan.enum_subsets:
                by_subset[(use.cls_name, use.members)].add(scan.path)
    found: list[Diagnostic] = []
    for scan in judged_scans:
        if is_test_file(scan.path):
            continue
        mode = effective_purity(scan.path, manifests)
        if mode not in {"strict", "strict-to-be"}:
            continue
        owner = scan.path.rpartition("/")[0]
        for use in scan.enum_subsets:
            paths = by_subset[(use.cls_name, use.members)]
            if len(paths) >= 2:
                key = f"{use.cls_name}({','.join(use.members)})"
                found.append(
                    Diagnostic(
                        scan.path,
                        use.line,
                        Code.DOMAIN_SUBSET_LEAK,
                        f"enum subset `{use.cls_name}.{{{', '.join(use.members)}}}` is branched on across {len(paths)} modules",
                        (
                            f"define a predicate method or `@property def is_...(self) -> bool` on `{use.cls_name}` so the domain concept has one owner",
                        ),
                        _purity_debt(owner, scan.path, key, mode),
                    )
                )
    return found


def purity_diagnostics(
    all_scans: Iterable[FileScan],
    judged_scans: list[FileScan],
    manifests: dict[str, Manifest],
    policy: Policy,
    *,
    include_manifests: bool = True,
    universe: Universe | None = None,
) -> list[Diagnostic]:
    scans_map: dict[str, FileScan] = {s.path: s for s in all_scans}
    for s in judged_scans:
        scans_map.setdefault(s.path, s)
    index = _build_fixture_index(scans_map, universe)
    pure_third_party = frozenset(policy.names("purity", "pure-third-party"))
    external_impure = DEFAULT_IMPURE_FIXTURES | frozenset(policy.names("purity", "impure-fixtures"))
    impure_fixtures = _infer_impure_fixtures(
        index,
        scans_map,
        manifests,
        pure_third_party,
        external_impure_fixtures=external_impure,
    )
    found: list[Diagnostic] = []
    for scan in judged_scans:
        found.extend(
            _file_purity_diagnostics(scan, manifests, impure_fixtures, index, external_impure)
        )
    found.extend(_domain_subset_diagnostics(all_scans, judged_scans, manifests))
    if include_manifests:
        found.extend(_wide_fanout_diagnostics(manifests, policy))
    return found


def stale_exports(
    manifests: dict[str, Manifest], projections: Iterable[Projection]
) -> tuple[list[Diagnostic], list[tuple[str, str]]]:
    traversed = {step for proj in projections for step in proj.steps}
    found: list[Diagnostic] = []
    stale: list[tuple[str, str]] = []
    for directory, manifest in sorted(manifests.items()):
        if "/" not in directory:
            continue
        for child in manifest.exports:
            if child in Universe.DOOR_FILES or (directory, child) in traversed:
                continue
            child_path = f"{directory}/{child}"
            mode = effective_purity(child_path, manifests)
            if mode not in {"strict", "strict-to-be"}:
                continue
            debt = _purity_debt(directory, child_path, "exports", mode)
            found.append(
                Diagnostic(
                    manifest.path,
                    manifest.line_of("exports"),
                    Code.STALE_EXPORT,
                    f"exports: {child!r} is exported by {_where(directory)} but no external fact edge traverses it",
                    ("run `python -m meizel sync --prune` to remove it",),
                    debt,
                )
            )
            if mode == "strict" or f"{child}#exports" not in manifest.debt.get(
                Code.STALE_EXPORT, {}
            ):
                stale.append((directory, child))
    return found, stale


def _has_purity_debt(manifests: dict[str, Manifest], prefix: str) -> bool:
    for owner, manifest in manifests.items():
        for code in PURITY_CODES:
            for key in manifest.debt.get(code, ()):
                src = source_of(owner, code, key)
                if not prefix or within(src, prefix):
                    return True
    return False


def _target_has_purity_diag(purity_diags: list[Diagnostic], directory: str, target: str) -> bool:
    for d in purity_diags:
        if within(d.path, target):
            return True
        if d.debt is not None and d.debt.owner == directory:
            src = source_of(directory, d.code, d.debt.key)
            if within(src, target):
                return True
    return False


def paid_strict_to_be(
    manifests: dict[str, Manifest], purity_diags: list[Diagnostic]
) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    for directory, manifest in sorted(manifests.items()):
        for child, level in sorted(manifest.purity.items()):
            if level != "strict-to-be":
                continue
            target = f"{directory}/{child}" if directory else child
            if not _target_has_purity_diag(
                purity_diags, directory, target
            ) and not _has_purity_debt(manifests, target):
                found.append(
                    Diagnostic(
                        manifest.path,
                        manifest.line_of(child),
                        Code.DEBT_PAID,
                        f"[purity] {child!r} is strict-to-be but 0 purity violations remain",
                        ('promote it to `"strict"`',),
                    )
                )
        if (
            manifest.purity_default == "strict-to-be"
            and not any(not directory or within(d.path, directory) for d in purity_diags)
            and not _has_purity_debt(manifests, directory)
        ):
            found.append(
                Diagnostic(
                    manifest.path,
                    manifest.line_of("purity"),
                    Code.DEBT_PAID,
                    f"purity of {directory or '.'} is strict-to-be but 0 purity violations remain",
                    ('promote `purity` to `"strict"`',),
                )
            )
    return found
