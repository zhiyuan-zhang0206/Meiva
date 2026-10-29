from __future__ import annotations

import ast

from meizel.state.ambient_state.scan import Hit
from meizel.state.ambient_state.tree import Tree

BUNDLE_LEAK = "bundle-leak"
FIX = (
    "a `@root_bundle` class stays in its composition root: unpack it there and pass the members "
    "each component needs (handles, slices) as arguments"
)
_DECORATOR = "root_bundle"


def _is_bundle(node: ast.ClassDef) -> bool:
    return any(
        (isinstance(d, ast.Name) and d.id == _DECORATOR)
        or (isinstance(d, ast.Attribute) and d.attr == _DECORATOR)
        for d in node.decorator_list
    )


# meiva:note
# The `@root_bundle` class names a module defines, read through the tree being judged and answered
# once per tree: every importer of a module asks this question, and reading the file once per
# importer was the largest repeated cost of a whole-tree ambient-state run (2026-10-29). Only an
# answer read and parsed from the tree is kept: a module the tree cannot read, or does not hold,
# is asked again next time, so a failed read can never be frozen into "defines no bundle".
def bundles_defined_in(index: Tree, module: str) -> frozenset[str]:
    cached = index.bundle_names.get(module)
    if cached is not None:
        return cached
    rel = index.module_path(module)
    text = None if rel is None else index.read(rel)
    if text is None:
        return frozenset()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return frozenset()
    found = frozenset(
        n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and _is_bundle(n)
    )
    index.bundle_names[module] = found
    return found


def _imported_bundles(tree: ast.Module, index: Tree) -> set[str]:
    """Local names the module imports that are bundle classes."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ImportFrom) and node.level == 0 and node.module):
            continue
        if index.module_path(node.module) is None:
            continue
        defined = bundles_defined_in(index, node.module)
        found.update(a.asname or a.name for a in node.names if a.name in defined)
    return found


def _annotations(tree: ast.Module) -> list[ast.expr]:
    out: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            every = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
            out += [a.annotation for a in every if a is not None and a.annotation is not None]
            if node.returns is not None:
                out.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            out.append(node.annotation)
    return out


def hits(tree: ast.Module, index: Tree) -> list[Hit]:
    """Every annotation of a bundle class imported from another module."""
    bundles = _imported_bundles(tree, index)
    if not bundles:
        return []
    return [
        Hit(BUNDLE_LEAK, node.id, node.lineno)
        for annotation in _annotations(tree)
        for node in ast.walk(annotation)
        if isinstance(node, ast.Name) and node.id in bundles
    ]
