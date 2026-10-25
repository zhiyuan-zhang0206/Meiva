"""Whole-repository analysis: facts, projections and every diagnostic."""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from meizel.contracts.ambient_read import ambient_diagnostics, sources_from, stale_roots
from meizel.contracts.debt import file_key, set_debt
from meizel.contracts.enumerations import check as check_roles
from meizel.contracts.ledger import debts_in, reconcile
from meizel.contracts.manifest import Manifest, load_manifests, manifest_path
from meizel.contracts.policy import POLICY, Policy, load_policy, render_policy
from meizel.contracts.policy import check as check_policy
from meizel.contracts.rule import (
    Projection,
    judge,
    paid_strict_to_be,
    project,
    purity_diagnostics,
    stale_exports,
)
from meizel.graph.model import Code, DebtKey, Diagnostic, Fact, FactKind, Patch, Sources
from meizel.graph.universe import Universe, load_universe
from meizel.language.fixtures import FixtureIndex, fixture_facts
from meizel.language.scan import EXTERNAL_FORMS, FileScan, mount_facts, scan_file
from meizel.roles import select_packages

__all__ = [
    "POLICY",
    "Analysis",
    "Policy",
    "analyze",
    "diagnose",
    "load_manifests",
    "load_policy",
    "render_policy",
    "scan_all",
    "set_debt",
    "sources_from",
    "type_checking_in_scope",
    "type_checking_violations",
]

_PARALLEL_FROM = 200
_WORKER: dict[str, tuple[Universe, Sources]] = {}


@dataclass(frozen=True, slots=True)
class Analysis:
    """Everything one run found.

    `diagnostics` holds every violation the rules found. `verdict` holds the ones that fail
    `check` once each directory's recorded debt is taken into account. `changed` is the set of
    files a `check --changed` run judged, or None for a whole-repository run.
    """

    universe: Universe
    manifests: dict[str, Manifest]
    scans: dict[str, FileScan]
    facts: list[Fact]
    projections: list[Projection]
    diagnostics: list[Diagnostic] = field(default_factory=list[Diagnostic])
    verdict: list[Diagnostic] = field(default_factory=list[Diagnostic])
    changed: frozenset[str] | None = None
    needed: dict[tuple[str, str, str], list[Fact]] = field(
        default_factory=dict[tuple[str, str, str], list[Fact]]
    )
    stale_edges: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])
    stale_loads: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    stale_externals: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    stale_exports: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def ok_line(self) -> str:
        debts = debts_in(self.manifests)
        scope = "" if self.changed is None else f"{len(self.changed)} changed paths, "
        carried = (
            ""
            if not debts
            else f"; {debts} recorded debts remain in "
            f"{sum(1 for m in self.manifests.values() if m.debt)} strict-to-be directories"
        )
        return (
            f"meizel: ok ({scope}{len(self.facts)} fact edges, "
            f"{len(self.manifests)} manifests{carried}); "
            "implemented checks passed for the reported scope; "
            "not a semantic purity proof or test-resource assessment"
        )


def _all_source_files(universe: Universe) -> tuple[str, ...]:
    policy, _ = load_policy(universe)
    scopes = (*policy.names("typescript", "scopes"), *policy.names("typescript", "import-sources"))
    exts = policy.names("typescript", "extensions") or (".ts", ".tsx")
    return universe.source_files(scopes, exts)


def _init_worker(universe: Universe, sources: Sources) -> None:
    _WORKER["scan"] = (universe, sources)


def _scan_in_worker(path: str) -> FileScan:
    universe, sources = _WORKER["scan"]
    return scan_file(universe, path, sources)


def scan_all(
    universe: Universe,
    sources: Sources,
    jobs: int | None = None,
    paths: Sequence[str] | None = None,
) -> dict[str, FileScan]:
    paths = _all_source_files(universe) if paths is None else paths
    workers = jobs if jobs is not None else min(os.cpu_count() or 1, 8)
    if workers <= 1 or len(paths) < _PARALLEL_FROM:
        return {p: scan_file(universe, p, sources) for p in paths}
    with ProcessPoolExecutor(
        workers, initializer=_init_worker, initargs=(universe, sources)
    ) as pool:
        scans = pool.map(_scan_in_worker, paths, chunksize=max(1, len(paths) // (workers * 8)))
        return {scan.path: scan for scan in scans}


def _load_facts(manifests: dict[str, Manifest]) -> list[Fact]:
    facts: list[Fact] = []
    for directory, manifest in manifests.items():
        for name, targets in manifest.loads.items():
            src = f"{directory}/{name}" if directory else name
            facts.extend(
                Fact(src, target, manifest.line_of(name), FactKind.LOAD, (), f"[loads] {target}")
                for target in targets
            )
    return facts


def _fixture_facts(
    universe: Universe, scans: dict[str, FileScan], judged: Collection[str]
) -> list[Fact]:
    defined = {
        path: scan.fixtures.defs | scan.imported
        for path, scan in scans.items()
        if scan.fixtures.defs or path.endswith("conftest.py") or scan.fixtures.plugins
    }
    plugins = {
        p: [n for n, _ in s.fixtures.plugins] for p, s in scans.items() if s.fixtures.plugins
    }
    requests = {
        p: s.fixtures.requests for p, s in scans.items() if s.fixtures.requests and p in judged
    }
    return fixture_facts(FixtureIndex(universe, defined, plugins), requests)


def _declared_loads(manifests: dict[str, Manifest]) -> set[str]:
    return {
        f"{directory}/{name}" if directory else name
        for directory, manifest in manifests.items()
        for name in manifest.loads
    }


def _declared_externals(manifests: dict[str, Manifest]) -> set[str]:
    return {
        f"{directory}/{name}" if directory else name
        for directory, manifest in manifests.items()
        for name in manifest.external
    }


def _source_diagnostics(
    scans: Iterable[FileScan], loaders: set[str], externals: set[str]
) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    for scan in scans:
        found.extend(Diagnostic(scan.path, p.line, p.code, p.message) for p in scan.problems)
        if scan.path in loaders:
            continue
        owner = scan.path.rpartition("/")[0]
        for item in scan.restricted:
            if scan.path in externals and item.form in EXTERNAL_FORMS:
                continue
            form = f"{item.form}{' ' + repr(item.detail) if item.detail else ''}"
            found.append(
                Diagnostic(
                    scan.path,
                    item.line,
                    Code.RESTRICTED,
                    f"{form} is outside the restricted Python subset",
                    (
                        "use a static import, and inject a collaborator instead of patching it; a file that genuinely "
                        f"owns a dynamic load declares it under [loads] in {manifest_path(scan.path.rpartition('/')[0])}; "
                        "a file that runs code from outside the repository declares it under [external] with a reason",
                    ),
                    DebtKey(owner, file_key(owner, scan.path, form)),
                )
            )
    return found


_PATCH_WHAT = {
    Code.FIRST_PARTY_PATCH: "first-party",
    Code.STDLIB_PATCH: "standard-library",
    Code.THIRD_PARTY_PATCH: "third-party",
    Code.ENV_PATCH: "the environment",
    Code.UNRESOLVED_PATCH: "an unresolved target",
}
_PATCH_HINT = {
    Code.FIRST_PARTY_PATCH: (
        "do not patch first-party code: split the code under test into a public facade and an "
        "implementation whose dependencies are explicit parameters, and let the test pass the "
        "fake in directly"
    ),
    Code.STDLIB_PATCH: (
        "do not patch the standard library: the code under test takes the dependency (a clock, a "
        "sleeper, a file opener) as an explicit parameter, and the test passes the fake in directly"
    ),
    Code.THIRD_PARTY_PATCH: (
        "do not patch a third-party package: wrap it behind a first-party seam whose dependency is "
        "an explicit parameter, and let the test pass the fake in directly"
    ),
    Code.ENV_PATCH: (
        "do not patch the environment: read the setting once at the boundary and pass the value "
        "in as an explicit parameter"
    ),
    Code.UNRESOLVED_PATCH: (
        "the target of this patch is not an imported module or object, so what it replaces is "
        "invisible to the checker: inject the dependency as an explicit parameter instead of "
        "patching it"
    ),
}
# meiva:note
# The two codes below carry their principle, recipe and rule as three lines; the target is zero,
# not a long-term debt.
_PATCH_REASONING = {
    Code.PATCH_RAISING_FALSE: (
        "why: `raising=False` turns a patch of a name that no longer exists into a silent no-op, so "
        "the test keeps passing while it stops testing what it claims",
        "instead: inject the dependency as a parameter; a name that is optional by design is "
        "asserted to exist before it is patched, or the test builds the object that lacks it. "
        "`missing-patch-target` is the same defect on a first-party module and cannot be recorded",
        "see: docs/conventions/python-conventions.md#test-seams-in-another-shape",
    ),
    Code.TEST_SUBCLASS_OVERRIDE: (
        "why: a test class that overrides one method of production code replaces behavior by "
        "name, a patch in another spelling; later changes of the base silently stop being tested",
        "instead: give the production class the value it reads as a constructor field and build "
        "the real class with it; lift the overridden decision into a pure function; make the "
        "fault with a real resource; only a fault no real resource can make takes a narrow "
        "Protocol in base/ with its fake in base's tests/",
        "see: docs/conventions/python-conventions.md#test-seams-in-another-shape",
    ),
}


def _patch_message(patch: Patch) -> str:
    if patch.code is Code.PATCH_RAISING_FALSE:
        return f"{patch.form}(..., raising=False) on {patch.target}"
    if patch.code is Code.TEST_SUBCLASS_OVERRIDE:
        return f"a test subclass overrides {patch.target}"
    suffix = " (imported binding)" if patch.imported else ""
    return f"{patch.form} on {_PATCH_WHAT[patch.code]} {patch.target}{suffix}"


def _patch_diagnostics(scans: Iterable[FileScan]) -> list[Diagnostic]:
    """Patching: the dependency was not injected."""
    found: list[Diagnostic] = []
    for scan in scans:
        owner = scan.path.rpartition("/")[0]
        for patch in scan.patches:
            hints = list(_PATCH_REASONING.get(patch.code) or (_PATCH_HINT[patch.code],))
            if patch.imported:
                hints.append(
                    "target is an imported binding on this module rather than defined here; "
                    "patch the defining module or inject the dependency"
                )
            found.append(
                Diagnostic(
                    scan.path,
                    patch.line,
                    patch.code,
                    _patch_message(patch),
                    tuple(hints),
                    DebtKey(owner, file_key(owner, scan.path, f"{patch.form} {patch.target}")),
                )
            )
    return found


def _stale_loads(scans: dict[str, FileScan], loaders: set[str]) -> list[tuple[str, str]]:
    return sorted(
        (path.rpartition("/")[0], path.rpartition("/")[2])
        for path in loaders
        if path not in scans or not scans[path].restricted
    )


def _stale_externals(scans: dict[str, FileScan], externals: set[str]) -> list[tuple[str, str]]:
    return sorted(
        (path.rpartition("/")[0], path.rpartition("/")[2])
        for path in externals
        if path not in scans or not any(r.form in EXTERNAL_FORMS for r in scans[path].restricted)
    )


def _stale_edges(
    manifests: dict[str, Manifest], needed: dict[tuple[str, str, str], list[Fact]]
) -> list[tuple[str, str, str]]:
    return [
        (directory, cf, ct)
        for directory, manifest in sorted(manifests.items())
        for cf, targets in manifest.uses.items()
        for ct in targets
        if (directory, cf, ct) not in needed
    ]


@dataclass(frozen=True, slots=True)
class StaleDeclarations:
    """Declared edges, loads and externals that nothing needs; only a whole run knows them."""

    edges: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])
    loads: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    externals: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    exports: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])


def _stale_diagnostics(
    manifests: dict[str, Manifest], stale: StaleDeclarations
) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    for directory, cf, ct in stale.edges:
        manifest = manifests[directory]
        found.append(
            Diagnostic(
                manifest.path,
                manifest.line_of(cf),
                Code.MANIFEST_STALE_EDGE,
                f"{cf} -> {ct} is declared but no fact edge needs it",
                ("run `python -m meizel sync --prune` to remove it",),
            )
        )
    for directory, name in stale.loads:
        manifest = manifests[directory]
        found.append(
            Diagnostic(
                manifest.path,
                manifest.line_of(name),
                Code.STALE_LOAD,
                f"[loads] {name!r} is declared but the file has no dynamic load",
                ("run `python -m meizel sync --prune` to remove it",),
            )
        )
    for directory, name in stale.externals:
        manifest = manifests[directory]
        found.append(
            Diagnostic(
                manifest.path,
                manifest.line_of(name),
                Code.STALE_EXTERNAL,
                f"[external] {name!r} is declared but the file runs no external code",
                ("remove the entry",),
            )
        )
    return found


def _scan_changed(
    universe: Universe, sources: Sources, changed: Collection[str], jobs: int | None
) -> dict[str, FileScan]:
    """The changed Python files, plus what resolving their fixture requests needs."""
    wanted = {p for p in _all_source_files(universe) if p in changed or p.endswith("conftest.py")}
    scans = scan_all(universe, sources, jobs, sorted(wanted))
    while True:
        modules = (
            universe.resolve_module(n) for s in scans.values() for n, _ in s.fixtures.plugins
        )
        more = sorted({m.file for m in modules if m is not None and m.file and m.file not in scans})
        if not more:
            return scans
        scans.update(scan_all(universe, sources, jobs, more))


def _distinct(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    """One diagnostic per site and message, as `check` prints them."""
    return list({(d.path, d.line, d.code, d.message): d for d in diagnostics}.values())


def _judge_facts(
    facts: list[Fact], manifests: dict[str, Manifest]
) -> tuple[list[Projection], list[Diagnostic], dict[tuple[str, str, str], list[Fact]]]:
    """Project every fact edge and judge it against the manifests."""
    projections = [p for p in map(project, facts) if p is not None]
    found: list[Diagnostic] = []
    needed: dict[tuple[str, str, str], list[Fact]] = defaultdict(list)
    for proj in projections:
        found.extend(judge(proj, manifests))
        manifest = manifests.get(proj.lca)
        if not proj.exempt and manifest is not None:
            for source, target in manifest.covering(*proj.relative()):
                needed[(proj.lca, source, target)].append(proj.fact)
    return projections, found, dict(needed)


def _type_cast_diagnostics(scans: Iterable[FileScan]) -> list[Diagnostic]:
    return [
        Diagnostic(
            scan.path,
            site.line,
            Code.TYPE_CAST,
            f"typing.cast call in {site.qualname} at byte column {site.column}",
            (
                "why: typing.cast asserts a type without validating the value",
                "instead: validate or narrow the value at its boundary",
                "see: meizel/docs/checker/type-cast.meiva.okf.md",
            ),
            DebtKey(
                scan.path.rpartition("/")[0],
                file_key(scan.path.rpartition("/")[0], scan.path, f"cast {site.qualname}"),
            ),
        )
        for scan in scans
        for site in scan.type_casts
    ]


def type_checking_in_scope(path: str, packages: tuple[str, ...]) -> bool:
    if path == "conftest.py":
        return True
    return (
        path.endswith(".py")
        and not path.rsplit("/", 1)[-1].startswith("test_")
        and path.split("/", 1)[0] in packages
    )


def type_checking_violations(
    lines: tuple[int, ...],
    rel_path: str,
    allowed: Collection[str],
    packages: tuple[str, ...],
) -> list[tuple[int, str]]:
    if rel_path in allowed or not type_checking_in_scope(rel_path, packages):
        return []
    return [
        (
            line,
            "`if TYPE_CHECKING:` is banned — import the real type at its light owner. "
            "Runtime type-hint introspection cannot resolve a type-only import; "
            "keep heavy construction in its factory rather than hiding its type.",
        )
        for line in lines
    ]


def _type_checking_diagnostics(
    scans: Iterable[FileScan], manifests: dict[str, Manifest], policy: Policy
) -> list[Diagnostic]:
    root = manifests.get("")
    packages = select_packages(
        root.roles if root else {}, "application", "tooling", "templates", "tests"
    )
    allowed = policy.reasons("type-checking", "files")
    return [
        Diagnostic(scan.path, line, Code.TYPE_CHECKING, message)
        for scan in scans
        for line, message in type_checking_violations(
            scan.type_checking, scan.path, allowed, packages
        )
    ]


# meiva:note
# The diagnostics a single file produces on its own, without whole-repository facts.
def diagnose(scan: FileScan, manifests: dict[str, Manifest], *, policy: Policy) -> list[Diagnostic]:
    _, found, _ = _judge_facts(scan.facts, manifests)
    loaders, externals = _declared_loads(manifests), _declared_externals(manifests)
    found.extend(_source_diagnostics([scan], loaders, externals))
    found.extend(_patch_diagnostics([scan]))
    found.extend(ambient_diagnostics([scan], manifests))
    found.extend(_type_cast_diagnostics([scan]))
    found.extend(_type_checking_diagnostics([scan], manifests, policy))
    found.extend(purity_diagnostics([scan], [scan], manifests, Policy(), include_manifests=False))
    return _distinct(found)


# meiva:note
# Analyse the repository, or with `changed` only the facts those files contribute.
#
# A partial run still reads every manifest, so the declared graphs are checked in full. It
# judges the changed files' fact edges, restricted forms and recorded debt, and every `[loads]`
# declaration, but not stale graph declarations: those need the whole repository's facts.
# Generated role lists and copies are checked globally in both modes, using this universe
# and the parsed root roles; they do not widen the fact scan.
def analyze(
    root: Path, *, jobs: int | None = None, changed: Collection[str] | None = None
) -> Analysis:
    universe = load_universe(root)
    manifests, diagnostics = load_manifests(universe)
    policy, policy_problems = load_policy(universe)
    diagnostics.extend(policy_problems)
    diagnostics.extend(check_policy(manifests, policy))
    root_manifest = manifests.get("")
    diagnostics.extend(check_roles(universe, root_manifest.roles if root_manifest else {}))
    scope = None if changed is None else frozenset(changed)
    sources = sources_from(manifests, policy)
    scans = (
        scan_all(universe, sources, jobs)
        if scope is None
        else _scan_changed(universe, sources, scope, jobs)
    )
    judged = sorted(scans if scope is None else scope & scans.keys())
    judged_scans = [scans[p] for p in judged]
    loaders = _declared_loads(manifests)
    externals = _declared_externals(manifests)
    facts = [f for scan in judged_scans for f in scan.facts]
    facts.extend(_fixture_facts(universe, scans, judged))
    facts.extend(_load_facts(manifests))
    mounts, unmounted = mount_facts(universe, scope)
    facts.extend(mounts)
    diagnostics.extend(unmounted)
    projections, found, needed = _judge_facts(facts, manifests)
    diagnostics.extend(found)
    diagnostics.extend(_source_diagnostics(judged_scans, loaders, externals))
    diagnostics.extend(_patch_diagnostics(judged_scans))
    diagnostics.extend(ambient_diagnostics(judged_scans, manifests))
    diagnostics.extend(_type_cast_diagnostics(judged_scans))
    diagnostics.extend(_type_checking_diagnostics(judged_scans, manifests, policy))
    purity_diags = purity_diagnostics(scans.values(), judged_scans, manifests, policy)
    stale = StaleDeclarations()
    if scope is None:
        export_diags, unused_exports = stale_exports(manifests, projections)
        purity_diags.extend(export_diags)
        stale = StaleDeclarations(
            _stale_edges(manifests, needed),
            _stale_loads(scans, loaders),
            _stale_externals(scans, externals),
            unused_exports,
        )
        diagnostics.extend(_stale_diagnostics(manifests, stale))
        diagnostics.extend(stale_roots(scans, manifests))
        diagnostics.extend(paid_strict_to_be(manifests, purity_diags))
    diagnostics.extend(purity_diags)
    diagnostics = _distinct(diagnostics)
    return Analysis(
        universe,
        manifests,
        scans,
        facts,
        projections,
        diagnostics,
        reconcile(diagnostics, manifests, lambda path: scope is None or path in scope),
        scope,
        needed,
        stale.edges,
        stale.loads,
        stale.externals,
        stale.exports,
    )
