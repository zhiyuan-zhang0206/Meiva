"""Whole-repository analysis: facts, projections and every diagnostic."""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

from meizel.contracts.ambient_read import (
    ambient_diagnostics,
    in_scope,
    reader_problems,
    sources_from,
    stale_readers,
    stale_roots,
)
from meizel.contracts.debt import (
    PATCH_HINT,
    PATCH_REASONING,
    PATCH_WHAT,
    DebtEntry,
    entry_value,
    file_key,
    read_entry,
    set_debt,
)
from meizel.contracts.enumerations import check as check_roles
from meizel.contracts.ledger import account_line, legal_read_recorded, reconcile
from meizel.contracts.manifest import Manifest, effective_purity, load_manifests, manifest_path
from meizel.contracts.policy import POLICY, Policy, load_policy, render_policy
from meizel.contracts.policy import check as check_policy
from meizel.contracts.rule import (
    Projection,
    judge_facts,
    paid_strict_to_be,
    purity_diagnostics,
    stale_exports,
)
from meizel.graph import python_reverse_closure
from meizel.graph.model import Code, DebtKey, Diagnostic, Fact, FactKind, Patch, Sources
from meizel.graph.universe import Universe, parent_of
from meizel.language import scan_all
from meizel.language.fixtures import FixtureIndex, fixture_facts, is_test_file
from meizel.language.scan import (
    EXTERNAL_FORMS,
    FileScan,
    mount_facts,
    typescript_files,
    typescript_scopes,
)
from meizel.lint_common import is_test_path
from meizel.roles import select_packages

__all__ = [
    "POLICY",
    "Analysis",
    "DebtEntry",
    "Policy",
    "analyze",
    "diagnose",
    "entry_value",
    "exempt_test_modules",
    "load_manifests",
    "load_policy",
    "manifest_diagnostics",
    "python_reverse_closure",
    "read_entry",
    "render_policy",
    "scan_all",
    "set_debt",
    "type_checking_in_scope",
    "type_checking_violations",
]


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
    # meiva:note
    # Warnings the run cannot turn into a violation: (manifest, the line to print).
    inert: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def language_counts(self) -> dict[str, int]:
        return {
            "python_ast": sum(path.endswith(".py") for path in self.scans),
            "typescript_compiler_ast": sum(not path.endswith(".py") for path in self.scans),
        }

    def coverage_line(self) -> str:
        counts = self.language_counts()
        return (
            f"meizel: analyzed {counts['python_ast']} Python files (stdlib AST), "
            f"{counts['typescript_compiler_ast']} TypeScript files (compiler AST); "
            "selection and rule coverage follow repository policy"
        )

    def ok_line(self) -> str:
        scope = "" if self.changed is None else f"{len(self.changed)} changed paths, "
        return (
            f"meizel: ok ({scope}{len(self.facts)} fact edges, "
            f"{len(self.manifests)} manifests{account_line(self.manifests)}); "
            "implemented checks passed for the reported scope; "
            "not a semantic purity proof or test-resource assessment"
        )


def _all_source_files(universe: Universe, policy: Policy) -> tuple[str, ...]:
    return (*universe.python_files, *typescript_files(universe, policy))


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
                    Code.RESTRICTED if scan.path.endswith(".py") else Code.UNRESOLVED_IMPORT,
                    f"{form} is outside the restricted Python subset"
                    if scan.path.endswith(".py")
                    else f"{form} has no declared TypeScript load targets",
                    (
                        "use a static import, and inject a collaborator instead of patching it; a file that genuinely "
                        f"owns a dynamic load declares it under [loads] in {manifest_path(scan.path.rpartition('/')[0])}; "
                        "a file that runs code from outside the repository declares it under [external] with a reason",
                    ),
                    DebtKey(owner, file_key(owner, scan.path, form))
                    if scan.path.endswith(".py")
                    else None,
                )
            )
    return found


_TEST_MODULE = re.compile(r"^test_|_test\.py$|\.(?:test|spec)\.")


# meiva:note
# `[approved-patches.files]`: the files that are allowed to patch, each with the reason. A patch is
# debt: the code under test should take its collaborator instead. A few test-support files patch on
# purpose, because their job is to keep the suite off the host or to put a child process in a state no
# parameter can reach: an autouse guard, the entry of a subprocess the test starts, the global setup of
# the test runner. The policy lists such a file by its path; none of its patches is then judged. Only
# fixture infrastructure qualifies: production code and test modules never do, so a test that patches
# is converted, not approved.
def _approved(policy: Policy) -> dict[str, str]:
    return policy.reasons("approved-patches", "files")


def _approval_refusal(path: str) -> str | None:
    if not is_test_path(path):
        return "is production code: approving a patch there would hide a dependency that should be injected"
    if _TEST_MODULE.search(path.rpartition("/")[2]):
        return "is a test module: a test that patches is converted, not approved"
    return None


# meiva:note
# An entry that cannot be approved (production code, a test module), whether the run is partial or whole.
def _approval_problems(policy: Policy) -> list[Diagnostic]:
    refused = {path: _approval_refusal(path) for path in _approved(policy)}
    return [
        Diagnostic(
            POLICY,
            policy.line_of(path, "approved-patches.files"),
            Code.POLICY_SYNTAX,
            f"[approved-patches.files] {path!r} {refusal}",
            ("approve only fixture infrastructure: a guard, a subprocess entry, a global setup",),
        )
        for path, refusal in refused.items()
        if refusal is not None
    ]


# meiva:note
# An approval that approves nothing fails like any other stale allowance: the file is gone, or it
# patches nothing any more. Only a whole-repository run knows.
def _stale_approvals(
    policy: Policy, files: Collection[str], patching: Mapping[str, int]
) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    for path in _approved(policy):
        if path not in files:
            problem = "names no tracked file"
        elif not patching.get(path):
            problem = "patches nothing any more"
        else:
            continue
        found.append(
            Diagnostic(
                POLICY,
                policy.line_of(path, "approved-patches.files"),
                Code.STALE_APPROVAL,
                f"[approved-patches.files] {path!r} {problem}",
                ("remove the entry",),
            )
        )
    return found


# meiva:note
# The modules the run's facts show as test code that nothing outside the tests imports, each named
# the way a patch target names it: a TypeScript file by its path, a Python module by its dotted name
# (`meizel/tests/codes/test_x.py` is `meizel.tests.codes.test_x`). An importer the run did not scan cannot
# clear the exemption: the answer is as complete as the run's fact graph. Public because the check's own
# test states this set and the production import that empties one entry.
def exempt_test_modules(facts: Iterable[Fact]) -> frozenset[str]:
    reached = [fact for fact in facts if fact.kind in (FactKind.IMPORT, FactKind.PATCH)]
    shipped = {fact.dst for fact in reached if not is_test_file(fact.src)}
    return frozenset(
        _target_name(fact.dst)
        for fact in reached
        if is_test_file(fact.dst) and fact.dst not in shipped
    )


# meiva:note
# Whether a target names one of these modules or a name bound inside one: a target adds the patched
# name to the module it reaches (`page-test-mock.tsx.activeId`, `lib.mod.helper`).
def _names_test_code(target: str, modules: Collection[str]) -> bool:
    candidate = target
    while candidate:
        if candidate in modules:
            return True
        candidate, separator, _ = candidate.rpartition(".")
        if not separator:
            return False
    return False


# meiva:note
# The module part of a patch target that names `path`: a TypeScript file keeps its path, a Python module
# is written dotted (`meizel/tests/codes/test_x.py` is `meizel.tests.codes.test_x`).
def _target_name(path: str) -> str:
    if not path.endswith(".py"):
        return path
    return path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")


# meiva:note
# How a patch reads: its form and the code's own word for what it targets.
def _patch_message(patch: Patch) -> str:
    if patch.code is Code.PATCH_RAISING_FALSE:
        return f"{patch.form}(..., raising=False) on {patch.target}"
    if patch.code is Code.TEST_SUBCLASS_OVERRIDE:
        return f"a test subclass overrides {patch.target}"
    suffix = " (imported binding)" if patch.imported else ""
    return f"{patch.form} on {PATCH_WHAT[patch.code]} {patch.target}{suffix}"


def _patch_hints(patch: Patch) -> tuple[str, ...]:
    hints = list(PATCH_REASONING.get(patch.code) or (PATCH_HINT[patch.code],))
    if patch.imported:
        hints.append(
            "target is an imported binding on this module rather than defined here; "
            "patch the defining module or inject the dependency"
        )
    return tuple(hints)


# meiva:note
# Patching: the dependency was not injected. A file the policy approves is not judged, and neither is
# a patch of test code: a test that drives its own mock reaches no collaborator of the code under
# test.
def _patch_diagnostics(
    scans: Iterable[FileScan], approved: Collection[str], facts: Iterable[Fact]
) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    test_code = exempt_test_modules(facts)
    for scan in scans:
        if scan.path in approved:
            continue
        owner = scan.path.rpartition("/")[0]
        for patch in scan.patches:
            if patch.code is Code.FIRST_PARTY_PATCH and _names_test_code(patch.target, test_code):
                continue
            found.append(
                Diagnostic(
                    scan.path,
                    patch.line,
                    patch.code,
                    _patch_message(patch),
                    _patch_hints(patch),
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


def _ancestor_conftests(universe: Universe, path: str) -> list[str]:
    found: list[str] = []
    directory = parent_of(path)
    while True:
        candidate = f"{directory}/conftest.py" if directory else "conftest.py"
        if candidate in universe.files:
            found.append(candidate)
        if not directory:
            return found
        directory = parent_of(directory)


def _needed_conftests(universe: Universe, scans: Mapping[str, FileScan]) -> list[str]:
    needed: set[str] = set()
    for scan in scans.values():
        if scan.fixtures.requests or scan.fixtures.plugins:
            needed.update(_ancestor_conftests(universe, scan.path))
    return sorted(needed - scans.keys())


def _missing_plugins(universe: Universe, scans: Mapping[str, FileScan]) -> list[str]:
    found: set[str] = set()
    for scan in scans.values():
        for name, _ in scan.fixtures.plugins:
            mod = universe.resolve_module(name)
            if mod is not None and mod.file is not None and mod.file not in scans:
                found.add(mod.file)
    return sorted(found)


# meiva:note
# Scans only changed source files first, pulling in ancestor conftest.py files and transitive
# pytest_plugins modules only when a changed file requests fixtures or declares plugins.
def _scan_changed(
    universe: Universe, sources: Sources, changed: Collection[str], jobs: int | None, policy: Policy
) -> dict[str, FileScan]:
    initial = sorted(p for p in _all_source_files(universe, policy) if p in changed)
    scans = scan_all(universe, sources, jobs, initial, policy=policy)
    conftests = _needed_conftests(universe, scans)
    if conftests:
        scans.update(scan_all(universe, sources, jobs, conftests, policy=policy))
    while more := _missing_plugins(universe, scans):
        scans.update(scan_all(universe, sources, jobs, more, policy=policy))
    return scans


def _distinct(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    """One diagnostic per site and message, as `check` prints them."""
    return list({(d.path, d.line, d.code, d.message): d for d in diagnostics}.values())


def _type_cast_diagnostics(scans: Iterable[FileScan]) -> list[Diagnostic]:
    return [
        Diagnostic(
            scan.path,
            site.line,
            Code.TYPE_CAST,
            f"{'typing.cast call' if scan.path.endswith('.py') else 'TypeScript type assertion'} in {site.qualname} at byte column {site.column}",
            (
                "why: a type assertion does not validate the value",
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


_WRAPPER_HINTS = (
    "why: a private function that is only `return time.monotonic()` hides the clock read from the "
    "rule on functions that wait and is a seam only a patch can reach; the module never hands it "
    "on, so nothing can inject the clock",
    "instead: take the clock as a required `Timers` parameter (or `now: float`) from the root, and "
    "delete the wrapper; one the module passes on as a default or an argument is an injection point",
    "see: meizel/docs/checker/fake-entropy.meiva.okf.md",
)
_ENTROPY_HINTS = (
    "why: a monotonic or perf-counter reading, an object address and a process-local counter are "
    "not entropy: two processes started together draw the same value, a restart repeats it and an "
    "address is reused; and because they are neither the wall clock nor a random call they hide a "
    "read of randomness from `ambient-read`",
    "instead: entropy has one carrier, the process's `random.Random`: pass it down from the "
    "composition root and mint identifiers with `new_uuid(rng)`; keep an interval clock for "
    "durations (`end - start`); a module's clock is a required `Timers` parameter, not a private "
    "wrapper",
    "see: meizel/docs/checker/fake-entropy.meiva.okf.md",
)


# meiva:note
# A reading spent as identity or randomness, or a private wrapper over an interval clock. It is
# recordable debt like an ambient read, except in a strict file.
def _entropy_diagnostics(
    scans: Iterable[FileScan], manifests: dict[str, Manifest]
) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    for scan in scans:
        if not scan.entropy or not in_scope(scan.path, manifests):
            continue
        owner = scan.path.rpartition("/")[0]
        strict = effective_purity(scan.path, manifests) == "strict"
        for site in scan.entropy:
            reading, _, context = site.what.partition(" in ")
            what = (
                f"`{site.what.split()[-1]}` wraps an interval clock that nothing injects"
                if site.what.startswith("interval-wrapper")
                else f"`{reading}` is spent as identity or randomness ({context})"
            )
            debt = None if strict else DebtKey(owner, file_key(owner, scan.path, site.what))
            hints = _WRAPPER_HINTS if site.what.startswith("interval-wrapper") else _ENTROPY_HINTS
            found.append(Diagnostic(scan.path, site.line, Code.FAKE_ENTROPY, what, hints, debt))
    return found


def _any_collaborator_diagnostics(scans: Iterable[FileScan]) -> list[Diagnostic]:
    return [
        Diagnostic(
            scan.path,
            site.line,
            Code.ANY_COLLABORATOR,
            f"{'typing.Any' if scan.path.endswith('.py') else 'any'} {site.role} annotation on {site.qualname}.{site.slot} at byte column {site.column}",
            (
                "why: an unrestricted Any type disables checking across collaborator boundaries",
                "instead: use a concrete collaborator type, a Protocol, or narrow object/Mapping values with TypeGuard/TypeIs or isinstance (do not use typing.cast, which [type-cast] forbids)",
                "see: meizel/docs/checker/any-collaborator.meiva.okf.md",
            ),
            DebtKey(
                scan.path.rpartition("/")[0],
                file_key(
                    scan.path.rpartition("/")[0], scan.path, f"any {site.qualname}.{site.slot}"
                ),
            ),
        )
        for scan in scans
        for site in scan.any_collaborators
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
        root.roles if root else {}, "application", "standalone", "tooling", "templates", "tests"
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
    _, found, _ = judge_facts(scan.facts, manifests)
    loaders, externals = _declared_loads(manifests), _declared_externals(manifests)
    found.extend(_source_diagnostics([scan], loaders, externals))
    found.extend(_patch_diagnostics([scan], _approved(policy), scan.facts))
    found.extend(ambient_diagnostics([scan], manifests))
    found.extend(_entropy_diagnostics([scan], manifests))
    found.extend(_type_cast_diagnostics([scan]))
    found.extend(_any_collaborator_diagnostics([scan]))
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
# meiva:note
# A `[roots]` table no rule can consult: an exemption is read when that file's own reads are judged,
# so a table in a directory this run never analyses exempts nothing while looking like a repaid read
# (#5691 probe B: adding it and deleting it left `check` byte-identical). It is a warning, never a
# violation: no rule forbids the table, the tool just cannot honour it.
def _inert_roots(
    manifests: Mapping[str, Manifest],
    *,
    analysed: Collection[str],
    tracked: Collection[str],
    scopes: tuple[str, ...],
) -> list[tuple[str, str]]:
    judged = frozenset(path.rpartition("/")[0] for path in analysed)
    source_dirs = {
        path.rpartition("/")[0] for path in tracked if path.rsplit("/", 1)[-1] != "meizel.toml"
    }
    covered = ", ".join(scopes) if scopes else "none declared"
    found: list[tuple[str, str]] = []
    for directory, manifest in sorted(manifests.items()):
        if not manifest.roots or directory in judged or directory not in source_dirs:
            continue
        names = ", ".join(f'"{name}"' for name in sorted(manifest.roots))
        line = min(manifest.line_of(name) for name in manifest.roots)
        where = directory or "the repository root"
        found.append(
            (
                manifest_path(directory),
                f"meizel: warning: {manifest_path(directory)}:{line}: [roots] {names} exempt "
                f"nothing: this run reads Python and the files inside [typescript].scopes "
                f"({covered}); no file directly in {where} is judged -- delete the entries, or "
                f"add {where} to the scopes so the TypeScript it names is read, unless a "
                f"`.d.ts` declaration is all it names, which no scope can make readable",
            )
        )
    return found


def _load_declarations(
    universe: Universe, scope: frozenset[str] | None
) -> tuple[dict[str, Manifest], Policy, list[Diagnostic]]:
    manifests, diagnostics = load_manifests(universe)
    policy, policy_problems = load_policy(universe)
    diagnostics.extend(policy_problems)
    diagnostics.extend(check_policy(manifests, policy))
    diagnostics.extend(reader_problems(policy))
    diagnostics.extend(legal_read_recorded(manifests))
    diagnostics.extend(_approval_problems(policy))
    root_manifest = manifests.get("")
    diagnostics.extend(
        check_roles(universe, root_manifest.roles if root_manifest else {}, changed=scope)
    )
    return manifests, policy, diagnostics


def manifest_diagnostics(universe: Universe) -> list[Diagnostic]:
    _, _, diagnostics = _load_declarations(universe, None)
    return _distinct(diagnostics)


def analyze(
    universe: Universe, *, jobs: int | None = None, changed: Collection[str] | None = None
) -> Analysis:
    scope = None if changed is None else frozenset(changed)
    manifests, policy, diagnostics = _load_declarations(universe, scope)
    sources = sources_from(manifests, policy, universe)
    scans = (
        scan_all(universe, sources, jobs, policy=policy)
        if scope is None
        else _scan_changed(universe, sources, scope, jobs, policy)
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
    projections, found, needed = judge_facts(facts, manifests)
    diagnostics.extend(found)
    diagnostics.extend(_source_diagnostics(judged_scans, loaders, externals))
    diagnostics.extend(_patch_diagnostics(judged_scans, _approved(policy), facts))
    diagnostics.extend(ambient_diagnostics(judged_scans, manifests))
    diagnostics.extend(_entropy_diagnostics(judged_scans, manifests))
    diagnostics.extend(_type_cast_diagnostics(judged_scans))
    diagnostics.extend(_any_collaborator_diagnostics(judged_scans))
    diagnostics.extend(_type_checking_diagnostics(judged_scans, manifests, policy))
    purity_diags = purity_diagnostics(scans.values(), judged_scans, manifests, policy)
    stale = StaleDeclarations()
    inert: list[tuple[str, str]] = []
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
        inert = _inert_roots(
            manifests,
            analysed=scans,
            tracked=universe.files,
            scopes=typescript_scopes(policy),
        )
        diagnostics.extend(stale_readers(universe, policy))
        diagnostics.extend(
            _stale_approvals(policy, universe.files, {p: len(s.patches) for p, s in scans.items()})
        )
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
        inert,
    )
