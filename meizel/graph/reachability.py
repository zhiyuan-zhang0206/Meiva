# meiva:note
# Reachability analysis and roots island garbage collection from declared [roots].
#
# Starting from all declared production roots ([roots] in meizel.toml), this module traverses
# the first-party static import graph (O(V + E) BFS reachability) to identify:
# 1. Disconnected code islands (0 incoming paths from production roots or test suites).
# 2. Fake-test-preserved islands (reachable ONLY from tests/fixtures, 0 paths from any [roots]).
# 3. Island clusters (connected components of dead or test-only preserved modules).
# meiva:link meizel/docs/meizel.meiva.okf.md

from __future__ import annotations

import re
from collections import defaultdict, deque
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from meizel.graph.model import Code, Diagnostic, Fact
from meizel.graph.universe import SOURCE_EXTENSIONS, Universe, parent_of

__all__ = [
    "DEFAULT_PROD_ROLES",
    "HasRoots",
    "IslandCluster",
    "ReachabilityReport",
    "ReachabilityScope",
    "is_test_path",
    "reachability_analysis",
]

DEFAULT_PROD_ROLES: tuple[str, ...] = ("application", "standalone", "templates")


# meiva:note
# Boundaries and options for reachability analysis.
@dataclass(frozen=True, slots=True)
class ReachabilityScope:
    explicit_roots: Sequence[str] | None = None
    target_roles: Sequence[str] = DEFAULT_PROD_ROLES
    target_paths: Sequence[str] | None = None
    include_inits: bool = False


_TEST_PATTERN = re.compile(
    r"(^|/)tests?/|"
    r"(^|/)test_[^/]+\.py$|"
    r"conftest\.py$|"
    r"_test\.py$|"
    r"\.(test|spec)\.(ts|tsx|mts|cts)$|"
    r"-mock\.(ts|tsx|mts|cts)$"
)


@runtime_checkable
class HasRoots(Protocol):
    @property
    def roots(self) -> Mapping[str, object]: ...


# meiva:note
# Whether a repo-relative POSIX path is a test module, directory, or test double.
def is_test_path(path: str) -> bool:
    return _TEST_PATTERN.search(path) is not None


def _within(path: str, targets: Collection[str]) -> bool:
    return any(not t or path == t or path.startswith(f"{t}/") for t in targets)


def _ancestor_inits(path: str, tracked_inits: Collection[str]) -> list[str]:
    inits: list[str] = []
    d = parent_of(path)
    while d:
        init = f"{d}/__init__.py"
        if init in tracked_inits:
            inits.append(init)
        d = parent_of(d)
    return inits


def _build_graph(facts: Sequence[Fact]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    forward: dict[str, set[str]] = defaultdict(set)
    reverse: dict[str, set[str]] = defaultdict(set)
    for f in facts:
        forward[f.src].add(f.dst)
        reverse[f.dst].add(f.src)
    return forward, reverse


def _bfs_reachable(
    seeds: Iterable[str],
    forward: Mapping[str, Collection[str]],
    tracked_inits: Collection[str],
) -> set[str]:
    reachable: set[str] = set()
    queue: deque[str] = deque()

    def _enqueue(node: str) -> None:
        if node not in reachable:
            reachable.add(node)
            queue.append(node)
            for init in _ancestor_inits(node, tracked_inits):
                if init not in reachable:
                    reachable.add(init)
                    queue.append(init)

    for seed in seeds:
        _enqueue(seed)

    while queue:
        curr = queue.popleft()
        for nxt in forward.get(curr, ()):
            _enqueue(nxt)

    return reachable


def _extract_manifest_roots(
    manifests: Mapping[str, object], prod_packages: Collection[str]
) -> list[str]:
    roots: list[str] = []
    for directory, manifest in manifests.items():
        if not isinstance(manifest, HasRoots):
            continue
        for name in manifest.roots:
            path = f"{directory}/{name}" if directory else name
            if is_test_path(path):
                continue
            top = path.split("/", 1)[0]
            if not prod_packages or top in prod_packages:
                roots.append(path)
    return roots


def _resolve_prod_packages(
    manifests: Mapping[str, object] | None, target_roles: Sequence[str]
) -> set[str]:
    root_manifest = manifests.get("") if manifests is not None else None
    roles: Mapping[str, tuple[str, ...]] = (
        getattr(root_manifest, "roles", {}) if root_manifest is not None else {}
    )
    prod_packages: set[str] = set()
    for role in target_roles:
        prod_packages.update(roles.get(role, ()))
    return prod_packages


def _resolve_candidate_files(
    universe: Universe, prod_packages: Collection[str], *, include_inits: bool
) -> set[str]:
    candidates: set[str] = set()
    for p in universe.files:
        if is_test_path(p) or not p.endswith(SOURCE_EXTENSIONS):
            continue
        if not include_inits and (p.endswith("/__init__.py") or p == "__init__.py"):
            continue
        top = p.split("/", 1)[0]
        if not prod_packages or top in prod_packages:
            candidates.add(p)
    return candidates


def _resolve_roots(
    universe: Universe,
    manifests: Mapping[str, object] | None,
    explicit_roots: Sequence[str] | None,
    prod_packages: Collection[str],
) -> tuple[str, ...]:
    if explicit_roots is not None:
        raw_roots = [r for r in explicit_roots if r in universe.files]
    elif manifests is not None:
        raw_roots = _extract_manifest_roots(manifests, prod_packages)
    else:
        raw_roots = []
    return tuple(sorted(set(raw_roots)))


def _resolve_test_files(universe: Universe, manifests: Mapping[str, object] | None) -> set[str]:
    root_manifest = manifests.get("") if manifests is not None else None
    roles: Mapping[str, tuple[str, ...]] = (
        getattr(root_manifest, "roles", {}) if root_manifest is not None else {}
    )
    test_dirs = set(roles.get("tests", ()))
    return {
        p
        for p in universe.files
        if is_test_path(p) or (test_dirs and p.split("/", 1)[0] in test_dirs)
    }


# meiva:note
# A connected component of unreachable production modules.
@dataclass(frozen=True, slots=True)
class IslandCluster:
    files: tuple[str, ...]
    is_test_preserved: bool
    test_importers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "files": list(self.files),
            "is_test_preserved": self.is_test_preserved,
            "test_importers": list(self.test_importers),
        }


def _build_cluster_adj(
    unreached: Collection[str], forward: Mapping[str, Collection[str]]
) -> dict[str, set[str]]:
    unreached_set = set(unreached)
    adj: dict[str, set[str]] = {node: set() for node in unreached_set}
    for u in unreached_set:
        for v in forward.get(u, ()):
            if v in unreached_set:
                adj[u].add(v)
                adj[v].add(u)
    return adj


def _traverse_component(
    start: str, adj: Mapping[str, Collection[str]], seen: set[str]
) -> tuple[str, ...]:
    comp: list[str] = []
    queue = [start]
    seen.add(start)
    while queue:
        curr = queue.pop()
        comp.append(curr)
        for nxt in adj.get(curr, ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return tuple(sorted(comp))


def _cluster_test_importers(
    comp_files: tuple[str, ...],
    reverse: Mapping[str, Collection[str]],
    test_files: Collection[str],
) -> tuple[str, ...]:
    importers: set[str] = set()
    for f in comp_files:
        for pred in reverse.get(f, ()):
            if pred in test_files or is_test_path(pred):
                importers.add(pred)
    return tuple(sorted(importers))


def _find_clusters(
    unreached: Collection[str],
    forward: Mapping[str, Collection[str]],
    reverse: Mapping[str, Collection[str]],
    test_reachable: Collection[str],
    test_files: Collection[str],
) -> list[IslandCluster]:
    unreached_set = set(unreached)
    if not unreached_set:
        return []

    adj = _build_cluster_adj(unreached_set, forward)
    seen: set[str] = set()
    clusters: list[IslandCluster] = []

    for node in sorted(unreached_set):
        if node in seen:
            continue
        comp_files = _traverse_component(node, adj, seen)
        is_preserved = any(f in test_reachable for f in comp_files)
        importers = _cluster_test_importers(comp_files, reverse, test_files)
        clusters.append(IslandCluster(comp_files, is_preserved, importers))

    return sorted(clusters, key=lambda c: (-len(c.files), c.files[0]))


def _make_fake_test_diagnostic(
    path: str,
    reverse: Mapping[str, Collection[str]],
    test_reachable: Collection[str],
    cluster_info: str,
) -> Diagnostic:
    direct_tests = sorted(
        pred for pred in reverse.get(path, ()) if is_test_path(pred) or pred in test_reachable
    )
    importers_info = f"test importers: {', '.join(direct_tests[:3])}" if direct_tests else ""
    hints = (
        "why: dead-code linters miss isolated clusters when tests import them; "
        "production code must be reachable from a production root in [roots]",
        "action: wire this module into a production assembly root, "
        "or delete this dead code island along with its fake-preserving tests",
        *(extra for extra in (importers_info, cluster_info) if extra),
    )
    return Diagnostic(
        path,
        1,
        Code.FAKE_TEST_ONLY_ISLAND,
        "production module is unreachable from any [roots] entrypoint, preserved only by tests",
        hints,
    )


def _make_disconnected_diagnostic(path: str, cluster_info: str) -> Diagnostic:
    hints = (
        "why: this module has no paths from any [roots] entrypoint or test suite",
        "action: safely delete this module (GC), or declare it in [roots] "
        "if it is a new entrypoint",
        *(extra for extra in (cluster_info,) if extra),
    )
    return Diagnostic(
        path,
        1,
        Code.DISCONNECTED_ISLAND,
        "production module has 0 incoming paths from production roots or test suites "
        "(disconnected dead code island)",
        hints,
    )


def _build_diagnostics(
    unreached: Collection[str],
    fake_test_islands: Collection[str],
    reverse: Mapping[str, Collection[str]],
    test_reachable: Collection[str],
    clusters_by_file: Mapping[str, IslandCluster],
    targets: Collection[str] | None,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    fake_set = set(fake_test_islands)

    for path in sorted(unreached):
        if targets is not None and not _within(path, targets):
            continue

        cluster = clusters_by_file.get(path)
        cluster_info = (
            f"island cluster ({len(cluster.files)} files): {', '.join(cluster.files[:4])}"
            if cluster is not None and len(cluster.files) > 1
            else ""
        )

        if path in fake_set:
            diagnostics.append(
                _make_fake_test_diagnostic(path, reverse, test_reachable, cluster_info)
            )
        else:
            diagnostics.append(_make_disconnected_diagnostic(path, cluster_info))

    return diagnostics


# meiva:note
# The result of reachability analysis across production modules.
@dataclass(frozen=True, slots=True)
class ReachabilityReport:
    roots: tuple[str, ...]
    reachable_production: tuple[str, ...]
    disconnected_islands: tuple[str, ...]
    fake_test_islands: tuple[str, ...]
    clusters: tuple[IslandCluster, ...]
    diagnostics: tuple[Diagnostic, ...]

    @property
    def is_clean(self) -> bool:
        return not self.disconnected_islands and not self.fake_test_islands

    def summary_line(self) -> str:
        return (
            f"meizel reachability: {len(self.roots)} production roots reach "
            f"{len(self.reachable_production)} production modules; "
            f"{len(self.disconnected_islands)} disconnected islands, "
            f"{len(self.fake_test_islands)} fake-test-only islands "
            f"across {len(self.clusters)} island clusters"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "roots": list(self.roots),
            "reachable_count": len(self.reachable_production),
            "reachable_production": list(self.reachable_production),
            "disconnected_islands": list(self.disconnected_islands),
            "fake_test_islands": list(self.fake_test_islands),
            "clusters": [c.to_dict() for c in self.clusters],
            "diagnostics": [
                {
                    "path": d.path,
                    "line": d.line,
                    "code": d.code.value,
                    "message": d.message,
                    "hints": list(d.hints),
                }
                for d in self.diagnostics
            ],
        }


def _partition_islands(
    unreached_prod: Collection[str], reachable_from_tests: Collection[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    fake_test = tuple(sorted(p for p in unreached_prod if p in reachable_from_tests))
    disconnected = tuple(sorted(p for p in unreached_prod if p not in reachable_from_tests))
    return fake_test, disconnected


def _unreached_analysis(
    universe: Universe,
    manifests: Mapping[str, object] | None,
    unreached_prod: set[str],
    graph: tuple[Mapping[str, Collection[str]], Mapping[str, Collection[str]]],
    tracked_inits: Collection[str],
    target_paths: Sequence[str] | None,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[IslandCluster, ...], tuple[Diagnostic, ...]]:
    forward, reverse = graph
    test_files = _resolve_test_files(universe, manifests)
    reachable_from_tests = _bfs_reachable(test_files, forward, tracked_inits)
    fake_test_islands, disconnected_islands = _partition_islands(
        unreached_prod, reachable_from_tests
    )
    clusters = tuple(
        _find_clusters(unreached_prod, forward, reverse, reachable_from_tests, test_files)
    )
    cluster_map = {f: c for c in clusters for f in c.files}
    diagnostics = tuple(
        _build_diagnostics(
            unreached_prod,
            fake_test_islands,
            reverse,
            reachable_from_tests,
            cluster_map,
            target_paths,
        )
    )
    return disconnected_islands, fake_test_islands, clusters, diagnostics


# meiva:note
# Traverse the first-party static import graph from production [roots] to detect dead islands.
def reachability_analysis(
    universe: Universe,
    *,
    facts: Sequence[Fact],
    manifests: Mapping[str, object] | None = None,
    scope: ReachabilityScope | None = None,
) -> ReachabilityReport:
    cfg = scope if scope is not None else ReachabilityScope()
    tracked_inits = frozenset(
        p for p in universe.files if p.endswith("/__init__.py") or p == "__init__.py"
    )

    prod_packages = _resolve_prod_packages(manifests, cfg.target_roles)
    candidates = _resolve_candidate_files(universe, prod_packages, include_inits=cfg.include_inits)
    roots = _resolve_roots(universe, manifests, cfg.explicit_roots, prod_packages)

    forward, reverse = _build_graph(facts)
    reachable_from_prod = _bfs_reachable(roots, forward, tracked_inits)
    unreached_prod = {p for p in candidates if p not in reachable_from_prod}

    reachable_tuple = tuple(sorted(candidates & reachable_from_prod))
    if not unreached_prod:
        return ReachabilityReport(roots, reachable_tuple, (), (), (), ())

    disconnected, fake_test, clusters, diagnostics = _unreached_analysis(
        universe,
        manifests,
        unreached_prod,
        (forward, reverse),
        tracked_inits,
        cfg.target_paths,
    )

    return ReachabilityReport(
        roots,
        reachable_tuple,
        disconnected,
        fake_test,
        clusters,
        diagnostics,
    )
