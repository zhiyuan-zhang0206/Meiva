from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import TypedDict

from meizel.contracts.analysis import Analysis, analyze
from meizel.contracts.debt import DebtTable, set_debt
from meizel.contracts.manifest import manifest_path
from meizel.contracts.rule import Projection
from meizel.graph.graphs import cycle_through, strongly_connected, transitive_reduction_size
from meizel.graph.model import Code, Fact, FactKind
from meizel.graph.universe import Checkout, EditPlan, Universe, is_private_module, parent_of

__all__ = ["Bootstrap", "bootstrap", "is_test_path", "plan_bootstrap", "render_manifest"]

Edges = dict[tuple[str, str], list[Fact]]


# meiva:note
# What `meizel bootstrap` reads from argv, checked once at the dispatch boundary: the directory to
# mirror manifests into and the worker count, instead of `Any` attributes of a parsed namespace.
@dataclass(frozen=True)
class Bootstrap:
    out: Path
    jobs: int | None


def is_test_path(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return (
        "tests" in parts[:-1]
        or "test-support" in parts[:-1]
        or name.startswith("test_")
        or name == "conftest.py"
        or name.endswith(
            (
                "_test.py",
                ".test.ts",
                ".test.tsx",
                ".spec.ts",
                ".spec.tsx",
                "-mock.ts",
                "-mock.tsx",
                ".test.mts",
                ".test.cts",
                ".spec.mts",
                ".spec.cts",
                "-mock.mts",
                "-mock.cts",
            )
        )
    )


def render_manifest(
    exports: set[str],
    uses: dict[str, set[str]],
    loads: dict[str, tuple[str, ...]],
    reasons: Mapping[str, dict[str, str]],
    debt: DebtTable | None = None,
    roles: dict[str, tuple[str, ...]] | None = None,
) -> str:
    def listing(items: list[str]) -> str:
        return "[" + ", ".join(f'"{item}"' for item in items) + "]"

    blocks: list[str] = []
    if exports:
        blocks.append(f"exports = {listing(sorted(exports))}")
    if uses:
        rows = "\n".join(f'"{src}" = {listing(sorted(uses[src]))}' for src in sorted(uses))
        blocks.append(f"[uses]\n{rows}")
    if loads:
        rows = "\n".join(f'"{src}" = {listing(sorted(loads[src]))}' for src in sorted(loads))
        blocks.append(f"[loads]\n{rows}")
    for table in ("external", "dynamic-attrs"):
        entries = reasons.get(table, {})
        if entries:
            rows = "\n".join(f'"{src}" = {json.dumps(entries[src])}' for src in sorted(entries))
            blocks.append(f"[{table}]\n{rows}")
    if roles:
        rows = "\n".join(f"{role} = {listing(list(names))}" for role, names in roles.items())
        blocks.append(f"[roles]\n{rows}")
    if debt:
        blocks.append(set_debt("", debt).rstrip("\n"))
    return "\n\n".join(blocks) + "\n"


def _edges_by_directory(projections: list[Projection]) -> dict[str, Edges]:
    grouped: dict[str, Edges] = defaultdict(lambda: defaultdict(list))
    for proj in projections:
        if not proj.exempt:
            grouped[proj.lca][(proj.cf, proj.ct)].append(proj.fact)
    return grouped


def _exports(projections: list[Projection]) -> tuple[dict[str, set[str]], list[dict[str, str]]]:
    exports: dict[str, set[str]] = defaultdict(set)
    blocked: dict[tuple[str, str], Fact] = {}
    for proj in projections:
        if proj.exempt:
            continue
        for position, (directory, child) in enumerate(proj.steps):
            if child in Universe.DOOR_FILES and position == len(proj.steps) - 1:
                continue
            if is_private_module(child):
                blocked.setdefault((directory, child), proj.fact)
            else:
                exports[directory].add(child)
    examples = [
        {"directory": d, "child": c, "fact": f"{f.src}:{f.line} -> {f.dst}"}
        for (d, c), f in sorted(blocked.items())
    ]
    return exports, examples


def _graph(edges: Edges, *, production: bool) -> dict[str, list[str]]:
    graph: dict[str, list[str]] = defaultdict(list)
    for (cf, ct), facts in edges.items():
        if not production or any(not is_test_path(f.src) for f in facts):
            graph[cf].append(ct)
    return graph


def _witness(fact: Fact) -> str:
    return f"{fact.src}:{fact.line} -> {fact.dst} ({fact.detail})"


# meiva:note
# The report `--out` writes as `report.json`: its shape is fixed by this module, so it is spelled out
# here instead of a `dict[str, Any]` that the summary line and every later reader would re-check.
class _Component(TypedDict):
    members: list[str]
    intra_edges: int
    cycle: list[str]
    witnesses: list[dict[str, str]]


class _DirectoryReport(TypedDict):
    children: int
    edges: int
    reduced_edges: int
    production_edges: int
    components: list[_Component]
    production_components: list[_Component]


class _Summary(TypedDict):
    source_files_by_backend: dict[str, int]
    directories_with_typescript_files: int
    directories_with_python_files: int
    directories_containing_python_anywhere: int
    directories_with_local_edges: int
    fact_edges: int
    fact_edges_by_kind: dict[FactKind, int]
    local_edges: int
    local_edges_reduced: int
    cyclic_directories: int
    cyclic_directories_production_only: int
    blocked_exports: int
    violations_by_code: dict[Code, int]


class _Report(TypedDict):
    summary: _Summary
    cyclic_directories: list[str]
    cyclic_directories_production_only: list[str]
    blocked_exports: list[dict[str, str]]
    directories: dict[str, _DirectoryReport]


def _components(graph: dict[str, list[str]], edges: Edges) -> list[_Component]:
    report: list[_Component] = []
    for members in strongly_connected(graph):
        cycle = cycle_through(graph, members)
        inside = set(members)
        report.append(
            {
                "members": sorted(members),
                "intra_edges": sum(1 for cf, ct in edges if cf in inside and ct in inside),
                "cycle": cycle,
                "witnesses": [
                    {"edge": f"{a} -> {b}", "fact": _witness(edges[(a, b)][0])}
                    for a, b in pairwise(cycle)
                ],
            }
        )
    return sorted(report, key=lambda c: -len(c["members"]))


def _directory_report(edges: Edges, children: int) -> _DirectoryReport:
    everything = _graph(edges, production=False)
    production = _graph(edges, production=True)
    return {
        "children": children,
        "edges": len(edges),
        "reduced_edges": transitive_reduction_size(everything),
        "production_edges": sum(len(v) for v in production.values()),
        "components": _components(everything, edges),
        "production_components": _components(production, edges),
    }


def _build_report(analysis: Analysis) -> _Report:
    universe = analysis.universe
    grouped = _edges_by_directory(analysis.projections)
    _, blocked = _exports(analysis.projections)
    directories = {
        d: _directory_report(e, len(universe.children.get(d, ())))
        for d, e in sorted(grouped.items())
    }
    python_dirs = {parent_of(p) for p in universe.python_files}
    above_python = {d for p in python_dirs for d in _ancestors(p)}
    cyclic = [d for d, r in directories.items() if r["components"]]
    cyclic_production = [d for d, r in directories.items() if r["production_components"]]
    return {
        "summary": {
            "source_files_by_backend": analysis.language_counts(),
            "directories_with_typescript_files": len(
                {parent_of(p) for p in universe.typescript_files}
            ),
            "directories_with_python_files": len(python_dirs),
            "directories_containing_python_anywhere": len(above_python),
            "directories_with_local_edges": len(directories),
            "fact_edges": len(analysis.facts),
            "fact_edges_by_kind": dict(Counter(f.kind for f in analysis.facts)),
            "local_edges": sum(r["edges"] for r in directories.values()),
            "local_edges_reduced": sum(r["reduced_edges"] for r in directories.values()),
            "cyclic_directories": len(cyclic),
            "cyclic_directories_production_only": len(cyclic_production),
            "blocked_exports": len(blocked),
            "violations_by_code": dict(Counter(d.code for d in analysis.diagnostics)),
        },
        "cyclic_directories": cyclic,
        "cyclic_directories_production_only": cyclic_production,
        "blocked_exports": blocked,
        "directories": directories,
    }


def _ancestors(directory: str) -> list[str]:
    parts = directory.split("/") if directory else []
    return ["/".join(parts[:i]) for i in range(len(parts) + 1)]


def plan_bootstrap(analysis: Analysis, out: Path) -> tuple[EditPlan, str]:
    grouped = _edges_by_directory(analysis.projections)
    exports, _ = _exports(analysis.projections)
    carried = {d: m.loads for d, m in analysis.manifests.items() if m.loads}
    externals = {d: m.external for d, m in analysis.manifests.items() if m.external}
    owners = {d: m.dynamic_attrs for d, m in analysis.manifests.items() if m.dynamic_attrs}
    debts = {d: m.debt for d, m in analysis.manifests.items() if m.debt}
    roles = {d: m.roles for d, m in analysis.manifests.items() if m.roles}
    writes: dict[Path, str] = {}
    for directory in sorted(
        set(grouped)
        | set(exports)
        | set(carried)
        | set(externals)
        | set(owners)
        | set(debts)
        | set(roles)
    ):
        uses: dict[str, set[str]] = defaultdict(set)
        for cf, ct in grouped.get(directory, {}):
            uses[cf].add(ct)
        target = out / manifest_path(directory)
        writes[target] = render_manifest(
            exports.get(directory, set()),
            uses,
            carried.get(directory, {}),
            {"external": externals.get(directory, {}), "dynamic-attrs": owners.get(directory, {})},
            debts.get(directory),
            roles.get(directory),
        )
    written = len(writes)
    report = _build_report(analysis)
    writes[out / "report.json"] = json.dumps(report, indent=2, sort_keys=True) + "\n"
    summary = report["summary"]
    message = f"meizel: wrote {written} manifests and report.json under {out}\n" + "\n".join(
        f"  {key}: {value}" for key, value in summary.items()
    )
    return EditPlan(writes=writes, dirs=(out,)), message


def bootstrap(repo: Checkout, out: Path, jobs: int | None = None) -> str:
    plan, message = plan_bootstrap(analyze(repo.universe(), jobs=jobs), out)
    repo.apply_plan(plan)
    return message
