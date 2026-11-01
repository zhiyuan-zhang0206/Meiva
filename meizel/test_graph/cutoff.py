from __future__ import annotations

import ast
import tomllib
from collections.abc import Iterable, Mapping
from typing import TypeGuard

from meizel.test_graph.model import CutoffContext

__all__ = [
    "effective_purity_from_manifests",
    "manifest_boundary_unchanged",
    "manifest_has_no_debt_for_file",
    "module_interface_unchanged",
    "package_local_tests",
    "strict_cutoff_eligible",
]

_BOUNDARY_KEYS = ("exports", "loads", "external", "state", "roots")


def _is_object(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _is_private_name(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _parse_manifest_doc(manifests: Mapping[str, str], directory: str) -> dict[str, object] | None:
    key = f"{directory}/meizel.toml" if directory else "meizel.toml"
    raw = manifests.get(key)
    if raw is None:
        return None
    try:
        return tomllib.loads(raw)
    except tomllib.TOMLDecodeError:
        return None


def _purity_from_doc(doc: Mapping[str, object], child: str) -> str | None:
    purity = doc.get("purity")
    if isinstance(purity, str):
        return purity
    if not _is_object(purity):
        return None
    child_mode = purity.get(child)
    if isinstance(child_mode, str):
        return child_mode
    if _is_object(child_mode):
        mode = child_mode.get("mode")
        if isinstance(mode, str):
            return mode
    default_mode = purity.get(".")
    return default_mode if isinstance(default_mode, str) else None


# meiva:note
# Resolves effective `meizel` purity mode for `path` by walking ancestor manifests to root.
def effective_purity_from_manifests(path: str, manifests: Mapping[str, str]) -> str:
    directory = path.rpartition("/")[0]
    while True:
        rel = path[len(directory) + 1 :] if directory else path
        child = rel.split("/", 1)[0]
        doc = _parse_manifest_doc(manifests, directory)
        if doc is not None:
            resolved = _purity_from_doc(doc, child)
            if resolved is not None:
                return resolved
        if not directory:
            return "none"
        directory = directory.rpartition("/")[0]


def _debt_mentions_target(key: str, rel: str, child: str) -> bool:
    base = key.split("#", 1)[0].strip()
    if " -> " in base:
        left, right = base.split(" -> ", 1)
        return left.strip() in (rel, child) or right.strip() in (rel, child)
    return base in (rel, child)


def _doc_has_target_debt(doc: Mapping[str, object], rel: str, child: str) -> bool:
    debt = doc.get("debt")
    if not _is_object(debt):
        return False
    for table in debt.values():
        if _is_object(table) and any(_debt_mentions_target(k, rel, child) for k in table):
            return True
    return False


# meiva:note
# Checks that no ancestor `meizel.toml` records `[debt.*]` entries targeting `path` or its child.
def manifest_has_no_debt_for_file(path: str, manifests: Mapping[str, str]) -> bool:
    directory = path.rpartition("/")[0]
    while True:
        rel = path[len(directory) + 1 :] if directory else path
        child = rel.split("/", 1)[0]
        doc = _parse_manifest_doc(manifests, directory)
        if doc is not None and _doc_has_target_debt(doc, rel, child):
            return False
        if not directory:
            return True
        directory = directory.rpartition("/")[0]


def manifest_boundary_unchanged(
    path: str,
    base_manifests: Mapping[str, str],
    head_manifests: Mapping[str, str],
) -> bool:
    directory = path.rpartition("/")[0]
    old_doc = _parse_manifest_doc(base_manifests, directory) or {}
    new_doc = _parse_manifest_doc(head_manifests, directory) or {}
    return all(old_doc.get(key) == new_doc.get(key) for key in _BOUNDARY_KEYS)


def _is_docstring(body: list[ast.stmt], index: int) -> bool:
    if index != 0:
        return False
    first = body[0]
    return (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    )


def _node_names(nodes: Iterable[ast.AST | None]) -> set[str]:
    return {
        child.id
        for node in nodes
        if node is not None
        for child in ast.walk(node)
        if isinstance(child, ast.Name)
    }


def _func_signature(
    node: ast.FunctionDef | ast.AsyncFunctionDef, referenced: set[str]
) -> tuple[object, ...]:
    referenced.update(
        _node_names([*node.decorator_list, *node.type_params, node.args, node.returns])
    )
    return (
        "func",
        type(node).__name__,
        node.name,
        tuple(ast.unparse(d) for d in node.decorator_list),
        tuple(ast.unparse(tp) for tp in node.type_params),
        ast.unparse(node.args),
        ast.unparse(node.returns) if node.returns is not None else None,
    )


def _exported_list_values(node: ast.expr | None) -> set[str]:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return set()
    return {
        elt.value
        for elt in node.elts
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
    }


def _ann_assign_signature(node: ast.AnnAssign, referenced: set[str]) -> tuple[object, ...] | str:
    target = node.target
    if not isinstance(target, ast.Name):
        return "invalid"
    if _is_private_name(target.id):
        return "private"
    referenced.update(_node_names([node.annotation, node.value]))
    if target.id in ("__all__", "__all_for_meiva__"):
        referenced.update(_exported_list_values(node.value))
    return (
        "ann_assign",
        target.id,
        ast.unparse(node.annotation),
        ast.unparse(node.value) if node.value is not None else None,
    )


def _plain_assign_signature(node: ast.Assign, referenced: set[str]) -> tuple[object, ...] | str:
    names: list[str] = []
    for target in node.targets:
        if not isinstance(target, ast.Name):
            return "invalid"
        names.append(target.id)
    public = tuple(name for name in names if not _is_private_name(name))
    if not public:
        return "private"
    if len(public) != len(names):
        return "invalid"
    referenced.update(_node_names([node.value]))
    if "__all__" in public or "__all_for_meiva__" in public:
        referenced.update(_exported_list_values(node.value))
    return ("assign", public, ast.unparse(node.value))


def _assign_signature(
    node: ast.Assign | ast.AnnAssign | ast.TypeAlias, referenced: set[str]
) -> tuple[object, ...] | str:
    if isinstance(node, ast.TypeAlias):
        alias_id = node.name.id
        if _is_private_name(alias_id):
            return "private"
        referenced.update(_node_names([*node.type_params, node.value]))
        return (
            "type_alias",
            alias_id,
            tuple(ast.unparse(tp) for tp in node.type_params),
            ast.unparse(node.value),
        )
    if isinstance(node, ast.AnnAssign):
        return _ann_assign_signature(node, referenced)
    return _plain_assign_signature(node, referenced)


def _class_signature(node: ast.ClassDef, referenced: set[str]) -> tuple[object, ...] | None:
    members = _scope_definitions(node.body, referenced, allow_imports=False)
    if members is None:
        return None
    referenced.update(
        _node_names([*node.decorator_list, *node.type_params, *node.bases, *node.keywords])
    )
    return (
        "class",
        node.name,
        tuple(ast.unparse(d) for d in node.decorator_list),
        tuple(ast.unparse(tp) for tp in node.type_params),
        tuple(ast.unparse(b) for b in node.bases),
        tuple(ast.unparse(k) for k in node.keywords),
        members,
    )


def _scope_definitions(
    body: list[ast.stmt], referenced: set[str], *, allow_imports: bool
) -> tuple[object, ...] | None:
    items: list[object] = []
    for index, stmt in enumerate(body):
        if _is_docstring(body, index) or isinstance(stmt, ast.Pass):
            continue
        if allow_imports and isinstance(stmt, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not _is_private_name(stmt.name):
                items.append(_func_signature(stmt, referenced))
        elif isinstance(stmt, ast.ClassDef):
            if not _is_private_name(stmt.name):
                sig = _class_signature(stmt, referenced)
                if sig is None:
                    return None
                items.append(sig)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.TypeAlias)):
            sig = _assign_signature(stmt, referenced)
            if sig == "invalid":
                return None
            if isinstance(sig, tuple):
                items.append(sig)
        else:
            return None
    return tuple(items)


def _record_alias(
    bound: str,
    target: str,
    alias: ast.alias,
    bindings: dict[str, str],
    reexports: set[tuple[str, str]],
) -> None:
    if _is_private_name(bound):
        return
    bindings[bound] = target
    if alias.asname == alias.name:
        reexports.add((bound, target))


def _import_bindings(
    tree: ast.Module,
) -> tuple[dict[str, str], frozenset[tuple[str, str]]] | None:
    bindings: dict[str, str] = {}
    reexports: set[tuple[str, str]] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                _record_alias(
                    alias.asname or alias.name.split(".", 1)[0],
                    alias.name,
                    alias,
                    bindings,
                    reexports,
                )
        elif isinstance(stmt, ast.ImportFrom):
            if any(alias.name == "*" for alias in stmt.names):
                return None
            prefix = "." * stmt.level + (stmt.module or "")
            for alias in stmt.names:
                _record_alias(
                    alias.asname or alias.name,
                    f"{prefix}:{alias.name}",
                    alias,
                    bindings,
                    reexports,
                )
    return bindings, frozenset(reexports)


def _module_interface_fingerprint(
    tree: ast.Module, *, is_package_init: bool
) -> tuple[object, ...] | None:
    referenced: set[str] = set()
    defs = _scope_definitions(tree.body, referenced, allow_imports=True)
    imports = _import_bindings(tree)
    if defs is None or imports is None:
        return None
    bindings, reexports = imports
    if is_package_init:
        interface_imports = tuple(sorted(bindings.items()))
    else:
        used = {(name, bindings[name]) for name in referenced if name in bindings} | set(reexports)
        interface_imports = tuple(sorted(used))
    if not defs and not interface_imports:
        return None
    return (defs, interface_imports)


# meiva:note
# Compares public AST interface fingerprints so private body edits under `"strict"` purity can
# prune downstream consumer tests outside the owning package.
def module_interface_unchanged(path: str, base_source: str, head_source: str) -> bool:
    if not path.endswith(".py"):
        return False
    try:
        base_tree = ast.parse(base_source, filename=path)
        head_tree = ast.parse(head_source, filename=path)
    except (SyntaxError, ValueError):
        return False
    is_init = path.endswith("/__init__.py")
    base_fp = _module_interface_fingerprint(base_tree, is_package_init=is_init)
    head_fp = _module_interface_fingerprint(head_tree, is_package_init=is_init)
    return base_fp is not None and base_fp == head_fp


def package_local_tests(
    path: str,
    test_dirs: frozenset[str],
    collectable: frozenset[str],
    *,
    include_root_tests: bool = False,
) -> set[str]:
    directory = path.rpartition("/")[0]
    while directory:
        tests_dir = directory if directory.rpartition("/")[2] == "tests" else f"{directory}/tests"
        if tests_dir in test_dirs:
            prefix = f"{tests_dir}/"
            return {test for test in collectable if test.startswith(prefix)}
        directory = directory.rpartition("/")[0]
    if include_root_tests and "tests" in test_dirs:
        return {test for test in collectable if test.startswith("tests/")}
    return set()


# meiva:note
# Requires `"strict"` purity with zero debt in both base and head, unchanged directory boundary
# manifests, non-empty package-local runtime tests on both sides, and identical public AST interface.
def strict_cutoff_eligible(
    path: str,
    ctx: CutoffContext,
    base_runtime_tests: set[str],
    head_runtime_tests: set[str],
    head_test_dirs: frozenset[str],
    head_collectable: frozenset[str],
) -> bool:
    parts = path.split("/")
    if not path.endswith(".py") or "tests" in parts[:-1] or parts[-1] == "conftest.py":
        return False
    base_source = ctx.base_sources.get(path)
    head_source = ctx.head_sources.get(path)
    if base_source is None or head_source is None:
        return False
    head_local = package_local_tests(path, head_test_dirs, head_collectable)
    base_local = package_local_tests(path, ctx.base_test_dirs, ctx.base_collectable)
    if not (head_local & head_runtime_tests) or not (base_local & base_runtime_tests):
        return False
    if (
        effective_purity_from_manifests(path, ctx.base_manifests) != "strict"
        or effective_purity_from_manifests(path, ctx.head_manifests) != "strict"
        or not manifest_has_no_debt_for_file(path, ctx.base_manifests)
        or not manifest_has_no_debt_for_file(path, ctx.head_manifests)
        or not manifest_boundary_unchanged(path, ctx.base_manifests, ctx.head_manifests)
    ):
        return False
    return module_interface_unchanged(path, base_source, head_source)
