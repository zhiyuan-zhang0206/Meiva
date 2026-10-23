"""The rule: project a fact edge onto its lowest common directory and judge it."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from meizel.contracts.debt import PURITY_CODES, file_key, source_of
from meizel.contracts.manifest import Manifest, effective_purity, manifest_path, within
from meizel.contracts.policy import Policy
from meizel.graph.graphs import reachable
from meizel.graph.model import Code, DebtKey, Diagnostic, Fact, FactKind
from meizel.graph.universe import is_private_module
from meizel.language.fixtures import DEFAULT_IMPURE_FIXTURES, is_test_file
from meizel.language.scan import FileScan

__all__ = [
    "Projection",
    "is_private",
    "judge",
    "paid_strict_to_be",
    "project",
    "purity_diagnostics",
    "stale_exports",
]


@dataclass(frozen=True, slots=True)
class Projection:
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


def project(fact: Fact) -> Projection | None:
    """None when the edge stays inside one node (a file, or a directory it loads from)."""
    src_parts = fact.src.split("/")
    dst_parts = fact.dst.split("/")
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
    return Projection(fact, "/".join(src_parts[:depth]), src_parts[depth], dst_parts[depth], steps)


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
        if child == "__init__.py" and position == last:
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
        exported = manifests[directory].exports if directory in manifests else ()
        if child not in exported:
            hint = f'add to {manifest_path(directory)}:  exports = [..., "{child}"]; or use the door `{_where(directory)}/__init__.py`'
            return Diagnostic(
                *_site(fact),
                Code.NOT_EXPORTED,
                f"{fact.edge()} ({fact.detail}) enters {_where(directory)} through {child!r}, which is not exported",
                (hint,),
                proj.debt_key(),
            )
    return None


def judge(proj: Projection, manifests: dict[str, Manifest]) -> list[Diagnostic]:
    """Every way one fact edge violates the three conditions of the rule."""
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
    found.extend(
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
    )
    return found


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
            effective_purity(fact.dst, manifests) == "none"
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


def _test_purity_diagnostics(
    scan: FileScan,
    owner: str,
    manifests: dict[str, Manifest],
    impure_fixtures: frozenset[str],
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
        if req.name in impure_fixtures
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
    impure_fixtures: frozenset[str],
) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    owner = scan.path.rpartition("/")[0]
    mode = effective_purity(scan.path, manifests)
    if mode in {"strict", "strict-to-be"}:
        found.extend(_module_purity_diagnostics(scan, owner, mode, manifests))
    if is_test_file(scan.path):
        found.extend(_test_purity_diagnostics(scan, owner, manifests, impure_fixtures))
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
) -> list[Diagnostic]:
    fixtures = DEFAULT_IMPURE_FIXTURES | frozenset(policy.names("purity", "impure-fixtures"))
    found: list[Diagnostic] = []
    for scan in judged_scans:
        found.extend(_file_purity_diagnostics(scan, manifests, fixtures))
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
            if child == "__init__.py" or (directory, child) in traversed:
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
