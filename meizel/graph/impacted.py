# meiva:note
# Reverse Python import closure over a repository snapshot, backed by the checkout SQLite cache.
# meiva:link meizel/docs/checker/checker.meiva.okf.md

from __future__ import annotations

import ast
import hashlib
import json
import os
from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from typing import TypeGuard

from meizel.graph.scopes import absolute_base, dotted_chain
from meizel.graph.universe import Checkout, Universe, parent_of

__all__ = ["python_reverse_closure"]

_PARALLEL_FROM = 200
_WORKER: dict[str, Universe] = {}


def _visit_import(
    view: Universe,
    node: ast.Import,
    targets: set[str],
    bound: dict[str, set[str]],
) -> None:
    for alias in node.names:
        if not view.is_first_party_name(alias.name):
            continue
        mod = view.resolve_module(alias.name)
        if mod is not None and mod.file is not None:
            targets.add(mod.file)
        if alias.asname:
            bound.setdefault(alias.asname, set()).add(view.canonical_name(alias.name))
        else:
            head = alias.name.split(".", 1)[0]
            bound.setdefault(head, set()).add(view.canonical_name(head))
            head_mod = view.resolve_module(head)
            if head_mod is not None and head_mod.file is not None:
                targets.add(head_mod.file)


def _visit_import_from(
    view: Universe,
    package: list[str],
    node: ast.ImportFrom,
    targets: set[str],
    bound: dict[str, set[str]],
) -> None:
    base = absolute_base(node, package)
    if base is None:
        return
    if not node.level and not view.is_first_party_name(base):
        return
    owner = view.resolve_module(base) if base else None
    for alias in node.names:
        if alias.name != "*":
            candidate = f"{base}.{alias.name}" if base else alias.name
            bound.setdefault(alias.asname or alias.name, set()).add(view.canonical_name(candidate))
            sub = view.resolve_module(candidate)
            if sub is not None:
                if sub.file is not None:
                    targets.add(sub.file)
                continue
        if owner is not None and owner.file is not None:
            targets.add(owner.file)


def _walk_module_attrs(view: Universe, origin: str, attrs: list[str], targets: set[str]) -> None:
    if not view.is_first_party_name(origin):
        return
    module = view.resolve_module(origin)
    current = origin
    for attr in attrs:
        if module is None:
            return
        deeper = view.resolve_module(f"{current}.{attr}")
        if deeper is None:
            if module.file is not None:
                targets.add(module.file)
            return
        current, module = f"{current}.{attr}", deeper
        if module.file is not None:
            targets.add(module.file)


def _visit_attributes(
    view: Universe,
    nodes: list[ast.AST],
    bound: dict[str, set[str]],
    targets: set[str],
) -> None:
    parent_values = {id(n.value) for n in nodes if isinstance(n, ast.Attribute)}
    for node in nodes:
        if not isinstance(node, ast.Attribute) or id(node) in parent_values:
            continue
        chain = dotted_chain(node)
        if chain is None:
            continue
        base_name, attrs = chain
        origins = bound.get(base_name.id)
        if not origins:
            continue
        for origin in origins:
            _walk_module_attrs(view, origin, attrs, targets)


def _extract_python_deps(universe: Universe, path: str) -> tuple[str, ...] | None:
    try:
        raw = universe.repo.load_bytes(path)
        raw.decode("utf-8")
        tree = ast.parse(raw, filename=path)
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return None
    view = universe.context(path)
    package = path.split("/")[:-1]
    targets: set[str] = set()
    bound: dict[str, set[str]] = {}
    nodes = list(ast.walk(tree))
    for node in nodes:
        if isinstance(node, ast.Import):
            _visit_import(view, node, targets, bound)
        elif isinstance(node, ast.ImportFrom):
            _visit_import_from(view, package, node, targets, bound)
    if bound:
        _visit_attributes(view, nodes, bound, targets)
    targets.discard(path)
    return tuple(sorted(targets))


def _layout_digest(lookup: Universe) -> str:
    hasher = hashlib.sha256()
    projects = [
        [item.directory, [[name, target] for name, target in item.packages]]
        for item in lookup.standalone_projects
    ]
    hasher.update(
        f"{lookup.repo.scanner_digest()}\0{json.dumps(projects, separators=(',', ':'))}\0".encode()
    )
    for path in lookup.python_files:
        hasher.update(f"p:{path}\0".encode())
    for directory in sorted(lookup.dirs):
        hasher.update(f"d:{directory}\0".encode())
    return hasher.hexdigest()


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _decode_deps(text: str) -> tuple[str, ...] | None:
    try:
        raw: object = json.loads(text)
    except ValueError:
        return None
    if not _is_object_list(raw):
        return None
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            return None
        out.append(item)
    return tuple(out)


def _init_worker(lookup: Universe) -> None:
    _WORKER["lookup"] = lookup


def _extract_in_worker(path: str) -> tuple[str, tuple[str, ...] | None]:
    return path, _extract_python_deps(_WORKER["lookup"], path)


def _extract_misses(
    lookup: Universe, paths: list[str], jobs: int | None
) -> dict[str, tuple[str, ...]] | None:
    workers = jobs if jobs is not None else min(os.cpu_count() or 1, 8)
    if workers <= 1 or len(paths) < _PARALLEL_FROM:
        pairs = [(p, _extract_python_deps(lookup, p)) for p in paths]
    else:
        with ProcessPoolExecutor(workers, initializer=_init_worker, initargs=(lookup,)) as pool:
            chunk = max(1, len(paths) // (workers * 8))
            pairs = list(pool.map(_extract_in_worker, paths, chunksize=chunk))
    found: dict[str, tuple[str, ...]] = {}
    for path, deps in pairs:
        if deps is None:
            return None
        found[path] = deps
    return found


def _partition_cached_deps(
    universe: Universe, lookup: Universe, paths: list[str]
) -> tuple[dict[str, str], dict[str, tuple[str, ...]], list[str]]:
    digests = universe.file_digests()
    if not digests:
        return {}, {}, list(paths)
    ctx = _layout_digest(lookup)
    keys = {p: f"pydeps:v1:{ctx}:{p}:{digests[p]}" for p in paths if p in digests}
    cached = universe.repo.cache_get_many(tuple(keys.values())) if keys else {}
    edges: dict[str, tuple[str, ...]] = {}
    misses: list[str] = []
    for path in paths:
        hit = cached.get(keys[path]) if path in keys else None
        decoded = None if hit is None else _decode_deps(hit)
        if decoded is not None:
            edges[path] = decoded
        else:
            misses.append(path)
    return keys, edges, misses


def _load_all_deps(
    universe: Universe, lookup: Universe, jobs: int | None
) -> dict[str, tuple[str, ...]] | None:
    paths = list(universe.python_files)
    if not paths:
        return {}
    keys, edges, misses = _partition_cached_deps(universe, lookup, paths)
    if not misses:
        return edges
    computed = _extract_misses(lookup, misses, jobs)
    if computed is None:
        return None
    edges.update(computed)
    updates = {
        keys[path]: json.dumps(deps, separators=(",", ":"))
        for path, deps in computed.items()
        if path in keys
    }
    if updates:
        universe.repo.cache_put_many(updates)
    return edges


def _add_ancestor_inits(
    path: str, src: str, existing: set[str], reverse: dict[str, set[str]]
) -> None:
    directory = parent_of(path)
    while directory:
        init = f"{directory}/__init__.py"
        if init in existing and init != src:
            reverse[init].add(src)
        directory = parent_of(directory)


def _build_reverse_graph(
    edges: dict[str, tuple[str, ...]], existing: set[str]
) -> dict[str, set[str]]:
    reverse: dict[str, set[str]] = defaultdict(set)
    for src, deps in edges.items():
        _add_ancestor_inits(src, src, existing, reverse)
        for dst in deps:
            reverse[dst].add(src)
            _add_ancestor_inits(dst, src, existing, reverse)
    return reverse


def _bfs_closure(
    reverse: dict[str, set[str]], seeds: list[str], existing: set[str]
) -> frozenset[str]:
    reached = set(seeds)
    frontier = set(seeds)
    while frontier:
        nxt: set[str] = set()
        for item in frontier:
            if item in reverse:
                nxt.update(reverse[item] - reached)
        reached |= nxt
        frontier = nxt
    return frozenset(reached & existing)


def _normalize_py_paths(changed_files: Sequence[str]) -> list[str]:
    return [
        norm
        for raw in changed_files
        if (norm := raw.replace("\\", "/").removeprefix("./").strip("/")).endswith(".py")
    ]


def python_reverse_closure(
    target: Universe | Checkout,
    changed_files: Sequence[str],
    *,
    jobs: int | None = None,
) -> frozenset[str] | None:
    py_changed = _normalize_py_paths(changed_files)
    if not py_changed:
        return frozenset()
    universe = target if isinstance(target, Universe) else target.universe()
    deleted = sorted(set(py_changed) - universe.files)
    lookup = (
        universe
        if not deleted
        else Universe(
            universe.repo,
            sorted(universe.files | set(deleted)),
            projects=universe.standalone_projects,
        )
    )
    edges = _load_all_deps(universe, lookup, jobs)
    if edges is None:
        return None
    existing = set(universe.python_files)
    reverse = _build_reverse_graph(edges, existing)
    return _bfs_closure(reverse, py_changed, existing)
