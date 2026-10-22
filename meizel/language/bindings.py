"""The names a first-party module binds at its top level, found without importing it."""

from __future__ import annotations

import ast
import builtins
import types
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path

__all__ = ["Bindings", "is_imported_binding", "module_bindings", "provides"]

# Names every module object has, and the builtins its code resolves when it binds no such name.
_ALWAYS = frozenset(dir(types.ModuleType("m"))) | frozenset(dir(builtins))
_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


@dataclass(frozen=True, slots=True)
class Bindings:
    """The top-level names of one module, and whether it can serve others at run time."""

    names: frozenset[str]
    dynamic: bool
    defined: frozenset[str] = frozenset()
    imported: frozenset[str] = frozenset()
    all_names: frozenset[str] = frozenset()


def provides(bindings: Bindings, name: str) -> bool:
    """Whether setting or deleting `name` on the module acts on something that exists."""
    return bindings.dynamic or name in bindings.names or name in _ALWAYS


def is_imported_binding(bindings: Bindings, name: str) -> bool:
    return (
        not bindings.dynamic
        and name in bindings.imported
        and name not in bindings.defined
        and name not in bindings.all_names
    )


def module_bindings(root: Path, file: str) -> Bindings:
    """The bindings of `root/file`; an unreadable or unparsable file is treated as dynamic."""
    try:
        stat = (root / file).stat()
    except OSError:
        return Bindings(frozenset(), dynamic=True)
    return _read(str(root / file), stat.st_mtime_ns, stat.st_size)


@cache
def _read(path: str, mtime_ns: int, size: int) -> Bindings:
    del mtime_ns, size  # the cache key: a rewritten file is read again
    try:
        tree = ast.parse(Path(path).read_bytes(), filename=path)
    except (OSError, SyntaxError, ValueError):
        return Bindings(frozenset(), dynamic=True)
    defined: set[str] = set()
    imported: set[str] = set()
    all_names: set[str] = set()
    dynamic = False
    for node in _in_module_scope(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            imported.update(_names_bound(node))
        else:
            defined.update(_names_bound(node))
        all_names.update(_all_entries(node))
        dynamic |= _serves_names(node)
    if path.endswith("__init__.py"):
        defined.add("__path__")  # the import system sets it on a package
    names = frozenset(defined | imported)
    return Bindings(
        names,
        dynamic,
        defined=frozenset(defined),
        imported=frozenset(imported),
        all_names=frozenset(all_names),
    )


def _all_entries(node: ast.AST) -> list[str]:
    value: ast.expr | None = None
    if (
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
    ) or (
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "__all__"
    ):
        value = node.value
    if isinstance(value, ast.List | ast.Tuple | ast.Set):
        return [
            e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
    return []


def _names_bound(node: ast.AST) -> list[str]:
    """The module-level names one node binds."""
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return [node.name, *_declared_global(node)]
    if isinstance(node, ast.Name):
        return [node.id] if isinstance(node.ctx, ast.Store) else []
    if isinstance(node, ast.Import):
        return [(a.asname or a.name).split(".")[0] for a in node.names]
    if isinstance(node, ast.ImportFrom):
        return [a.asname or a.name for a in node.names]
    if isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
        return [node.name] if node.name else []
    if isinstance(node, ast.MatchMapping):
        return [node.rest] if node.rest else []
    return []


def _serves_names(node: ast.AST) -> bool:
    """Whether one node lets the module answer for names it does not bind."""
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return node.name == "__getattr__"
    if isinstance(node, ast.ImportFrom):
        return any(a.name == "*" for a in node.names)
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Name) and node.func.id == "globals"
    return False


def _in_module_scope(tree: ast.Module) -> Iterator[ast.AST]:
    """Every node that runs in the module's own namespace: nested scopes are yielded, not entered."""
    stack: list[ast.AST] = list(reversed(tree.body))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _NESTED):
            stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _declared_global(function: ast.AST) -> Iterator[str]:
    """Names a function declares `global`: it binds them in the module when it runs."""
    if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
        return
    for node in ast.walk(function):
        if isinstance(node, ast.Global):
            yield from node.names
