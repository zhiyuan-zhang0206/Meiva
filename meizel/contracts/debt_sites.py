"""`debt sites`: every place in the source that a code that names a target is reported.

The baselines count violations per file and key; they do not say where. A codemod that removes
one kind of patch, and a design that needs the list of its sites, ask the source: this scans the
files and lists each patch with its file, line, form and target, optionally for the targets under
some prefixes. It reports what the source holds now, whether or not the baseline records it.
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass

from meizel.contracts.ambient_read import in_scope
from meizel.contracts.analysis import load_policy, scan_all
from meizel.contracts.debt import PATCH_CODES
from meizel.contracts.manifest import Manifest
from meizel.graph.imports import Scope, collect_scope, dotted_chain
from meizel.graph.model import Code, MeizelError, Sources
from meizel.graph.universe import Universe
from meizel.language.scan import FileScan
from meizel.lint_common import is_test_path

__all__ = [
    "Approval",
    "Site",
    "approved",
    "format_approved",
    "format_sites",
    "propagation",
    "sites",
    "target_under",
]


# meiva:note
# Whether the dotted `target` is `prefix` or inside it (`lib.mod` holds `lib.mod.f`, not `lib.mod2`).
def target_under(target: str, prefix: str) -> bool:
    return target == prefix or target.startswith((f"{prefix}.", f"{prefix}/"))


@dataclass(frozen=True, slots=True)
class Site:
    path: str
    line: int
    code: str
    form: str
    target: str


def _code(name: str) -> Code:
    codes = ", ".join(c.value for c in PATCH_CODES)
    for code in PATCH_CODES:
        if code.value == name:
            return code
    raise MeizelError(f"{name!r}: no sites are listed for this code; the codes are {codes}")


def _within(path: str, scopes: list[str]) -> bool:
    return not scopes or any(path == s or path.startswith(f"{s.rstrip('/')}/") for s in scopes)


def sites(
    universe: Universe,
    code: str,
    prefixes: list[str],
    paths: list[str],
    jobs: int | None = None,
) -> list[Site]:
    """The sites of `code` whose target is under one of the prefixes (any, when none is given).

    Sorted by target, then file and line, so the same tree always lists the same way.
    """
    wanted = _code(code)
    scopes = [p.rstrip("/") for p in paths]
    files = [p for p in (*universe.python_files, *universe.typescript_files) if _within(p, scopes)]
    found: list[Site] = []
    policy, problems = load_policy(universe)
    if problems:
        raise MeizelError(problems[0].render())
    for path, scan in sorted(scan_all(universe, Sources(), jobs, files, policy=policy).items()):
        found.extend(
            Site(path, patch.line, wanted.value, patch.form, patch.target)
            for patch in scan.patches
            if patch.code is wanted
            and (not prefixes or any(target_under(patch.target, prefix) for prefix in prefixes))
        )
    return sorted(found, key=lambda s: (s.target, s.path, s.line, s.form))


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def format_sites(found: list[Site], *, as_json: bool) -> str:
    if as_json:
        return json.dumps([asdict(site) for site in found], indent=2)
    lines = [f"{s.path}:{s.line}: {s.form} {s.target}" for s in found]
    files = len({s.path for s in found})
    lines.append(f"meizel: {_count(len(found), 'site')} in {_count(files, 'file')}")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Approval:
    path: str
    sites: int
    why: str


# meiva:note
# The files the policy approves to patch, each with its reason and the patches it holds now. These
# are not debt and no manifest records them: this is the list a reviewer reads to see them grow.
def approved(universe: Universe, jobs: int | None = None) -> list[Approval]:
    policy, problems = load_policy(universe)
    if problems:
        raise MeizelError(problems[0].render())
    listed = policy.reasons("approved-patches", "files")
    present = [path for path in listed if path in universe.files]
    scans = scan_all(universe, Sources(), jobs, present, policy=policy)
    return [
        Approval(path, len(scans[path].patches) if path in scans else 0, why)
        for path, why in sorted(listed.items())
    ]


def format_approved(found: list[Approval], *, as_json: bool) -> str:
    if as_json:
        return json.dumps([asdict(item) for item in found], indent=2)
    lines = [f"{item.path}: {_count(item.sites, 'site')} -- {item.why}" for item in found]
    total = sum(item.sites for item in found)
    lines.append(
        f"meizel: {_count(total, 'patch site')} in {_count(len(found), 'approved file')}, not recorded as debt"
    )
    return "\n".join(lines)


# meiva:note
# `ambient-read` records the point of acquisition: the file that calls `os.geteuid()`, `time.time()`
# or a `[state]` name. It does not follow the value out of that function, so a caller of
# `ensure_private_dir()` that hands it no `euid` inherits the world read and records nothing -- and
# neither does a caller above it. This is the measurement of that surface, and it writes nothing: no
# baseline, total or ratchet reads it, and the count it prints is an upper bound (a call that does
# pass the value cannot be told apart syntactically and is counted too).


@dataclass(frozen=True, slots=True)
class _Site:
    path: str
    line: int
    wrapper: str


@dataclass(frozen=True, slots=True)
class _Wrapper:
    name: str
    path: str
    line: int
    sites: int
    acquires: bool


@dataclass(frozen=True, slots=True)
class _Propagation:
    sites: list[_Site]
    wrappers: list[_Wrapper]

    # meiva:note
    # The measurement as lines: the count, the widest wrappers, then every site. The first line says
    # what the number is, because it is the one line somebody quotes later.
    def render(self, *, top: int) -> str:
        inherited = sum(1 for wrapper in self.wrappers if not wrapper.acquires)
        lines = [
            f"meizel: world reads that travel through a function: {len(self.sites)} sites "
            f"(upper bound: a call that passes the value counts too; not in the ledger) "
            f"reaching {len(self.wrappers)} functions, {inherited} of them only by calling another"
        ]
        if not self.sites:
            lines.append("meizel: no production call reaches a function that acquires the world")
            return "\n".join(lines)
        lines.append(f"meizel: the {min(top, len(self.wrappers))} widest:")
        lines.extend(
            f"meizel: {wrapper.sites:5d}  {wrapper.path}:{wrapper.line}  {wrapper.name}"
            f"{'' if wrapper.acquires else '  (through a call)'}"
            for wrapper in self.wrappers[:top]
        )
        lines.append("meizel: the sites:")
        lines.extend(f"meizel: {site.path}:{site.line}  -> {site.wrapper}" for site in self.sites)
        return "\n".join(lines)


# meiva:note
# The dotted module a file defines (`pkg/mod.py` -> `pkg.mod`, `pkg/__init__.py` -> `pkg`).
def _dotted(path: str) -> str:
    parts = path.removesuffix(".py").split("/")
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _tree(universe: Universe, path: str) -> ast.Module | None:
    try:
        return ast.parse(universe.repo.load_text(path), filename=path)
    except (SyntaxError, ValueError):
        return None


# meiva:note
# Every node of a module with the qualified name of the function that holds it, "" at module level:
# a class keeps its methods' names (`Class.method`), a nested function its outer's (`outer.inner`).
def _nodes(
    body: Sequence[ast.AST], prefix: str = "", current: str = ""
) -> Iterator[tuple[str, ast.AST]]:
    for node in body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            name = f"{prefix}{node.name}"
            yield name, node
            yield from _nodes(node.body, f"{name}.", name)
        elif isinstance(node, ast.ClassDef):
            yield current, node
            yield from _nodes(node.body, f"{prefix}{node.name}.", current)
        else:
            yield current, node
            yield from _nodes(list(ast.iter_child_nodes(node)), prefix, current)


def _spans(tree: ast.Module) -> list[tuple[int, int, str]]:
    return [
        (node.lineno, node.end_lineno or node.lineno, name)
        for name, node in _nodes(tree.body)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


def _enclosing(spans: list[tuple[int, int, str]], line: int) -> str:
    inside = [span for span in spans if span[0] <= line <= span[1]]
    return min(inside, key=lambda span: span[1] - span[0])[2] if inside else ""


def _module_scope(tree: ast.Module, path: str) -> Scope:
    scope = Scope(None)
    collect_scope(scope, tree.body, path.split("/")[:-1], walrus=True)
    return scope


def _module_level(tree: ast.Module) -> set[str]:
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }


def _callee(func: ast.expr, scope: Scope, module: str, defined: set[str]) -> str | None:
    chain = dotted_chain(func)
    if chain is None:
        return None
    base, attrs = chain
    origin, _ = scope.find(base.id)
    if origin is not None:
        return ".".join([origin, *attrs])
    return f"{module}.{base.id}" if not attrs and base.id in defined else None


# meiva:note
# The governed production files this measurement reads: a file declared under `[roots]` reads by
# design -- that is what a composition root is for -- so what leaves it was handed down on purpose,
# and a test records no acquisition at all.
def _measured_files(manifests: Mapping[str, Manifest], scans: Mapping[str, FileScan]) -> list[str]:
    roots = {
        f"{owner}/{path}" if owner else path for owner, m in manifests.items() for path in m.roots
    }
    return [
        path
        for path in sorted(scans)
        if path not in roots and not is_test_path(path) and in_scope(path, manifests)
    ]


def _read(
    universe: Universe, path: str, acquisitions: Iterable[int]
) -> tuple[dict[str, str], dict[str, tuple[str, int]], list[tuple[str, str, str, int]]]:
    tree = _tree(universe, path)
    if tree is None:
        return {}, {}, []
    module = _dotted(path)
    scope = _module_scope(tree, path)
    defined = _module_level(tree)
    spans = _spans(tree)
    holders = {
        f"{module}.{holder}"
        for holder in {_enclosing(spans, line) for line in acquisitions}
        if holder
    }
    found = {
        f"{module}.{name}": (path, node.lineno)
        for name, node in _nodes(tree.body)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    calls = [
        (f"{module}.{current}" if current else module, callee, path, node.lineno)
        for current, node in _nodes(tree.body)
        if isinstance(node, ast.Call)
        for callee in [_callee(node.func, scope, module, defined)]
        if callee is not None
    ]
    return dict.fromkeys(holders, path), found, calls


# meiva:note
# The functions that reach an acquisition through calls: a function that calls one inherits its
# read, and so does the caller of that caller, at any depth.
def _closure(acquiring: set[str], calls: list[tuple[str, str, str, int]]) -> set[str]:
    inside = set(acquiring)
    edges: dict[str, set[str]] = {}
    for caller, callee, _, _ in calls:
        edges.setdefault(caller, set()).add(callee)
    changed = True
    while changed:
        changed = False
        for caller, callees in edges.items():
            if caller not in inside and not callees.isdisjoint(inside):
                inside.add(caller)
                changed = True
    return inside


# meiva:note
# The world reads a call site inherits through a function, and where they enter the governed code.
def propagation(
    universe: Universe, manifests: Mapping[str, Manifest], scans: Mapping[str, FileScan]
) -> _Propagation:
    acquiring: dict[str, str] = {}
    defined: dict[str, tuple[str, int]] = {}
    calls: list[tuple[str, str, str, int]] = []
    for path in _measured_files(manifests, scans):
        holders, functions, edges = _read(universe, path, (a.line for a in scans[path].ambient))
        acquiring.update(holders)
        defined.update(functions)
        calls.extend(edges)
    inside = _closure(set(acquiring), calls)
    sites = sorted(
        (
            _Site(path, line, callee)
            for caller, callee, path, line in calls
            if callee in inside and caller not in acquiring
        ),
        key=lambda site: (site.path, site.line),
    )
    counted = Counter(site.wrapper for site in sites)
    wrappers = sorted(
        (
            _Wrapper(name, defined[name][0], defined[name][1], counted[name], name in acquiring)
            for name in inside
            if name in defined and counted[name]
        ),
        key=lambda wrapper: (-wrapper.sites, wrapper.name),
    )
    return _Propagation(sites, wrappers)
