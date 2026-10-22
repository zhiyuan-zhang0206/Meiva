from __future__ import annotations

import ast

from meizel.state.ambient_state import allowlist as allow
from meizel.state.ambient_state.scan import Hit

AMBIENT_DB = "ambient-db"
FIX = (
    f"take a `{allow.DB_HANDLE_CLASS}` (or its pool/connection) from the composition root named in "
    "DB_HANDLE_PACKAGES instead of dialing from the live settings; only the root calls "
    f"`{allow.DB_HANDLE_CLASS}.from_settings()`"
)


def package_of(rel: str) -> str | None:
    return next((pkg for pkg in allow.DB_HANDLE_PACKAGES if rel.startswith(f"{pkg}/")), None)


class _Names:
    def __init__(self, tree: ast.Module) -> None:
        self.modules: set[str] = set(allow.DB_MODULES)
        self.primitives: dict[str, str] = {}
        self.databases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in allow.DB_MODULES and alias.asname:
                        self.modules.add(alias.asname)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                self._from_import(node)

    def _from_import(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            local = alias.asname or alias.name
            for mod in allow.DB_MODULES:
                if mod.count(".") == 1:
                    parent, _, leaf = mod.partition(".")
                    if node.module == parent and alias.name == leaf:
                        self.modules.add(local)
            if node.module in allow.DB_MODULES and alias.name in allow.DB_PRIMITIVES:
                self.primitives[local] = alias.name
            elif node.module in allow.DB_HANDLE_MODULES and alias.name == allow.DB_HANDLE_CLASS:
                self.databases.add(local)


def _call_hit(call: ast.Call, names: _Names, *, is_root: bool) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, (ast.Name, ast.Attribute)):
        owner = ast.unparse(func.value)
        if owner in names.modules and func.attr in allow.DB_PRIMITIVES:
            return func.attr
        if owner in names.databases and func.attr == "from_settings" and not is_root:
            return f"{allow.DB_HANDLE_CLASS}.from_settings"
    elif isinstance(func, ast.Name):
        if func.id in names.primitives:
            return names.primitives[func.id]
    return None


def dials(tree: ast.Module, *, is_root: bool = False) -> list[Hit]:
    """Every ambient database dial in one module, whatever package it belongs to."""
    names = _Names(tree)
    return [
        Hit(AMBIENT_DB, name, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (name := _call_hit(node, names, is_root=is_root)) is not None
    ]


def hits(tree: ast.Module, rel: str) -> list[Hit]:
    """Every ambient database dial in a governed module."""
    package = package_of(rel)
    if package is None:
        return []
    return dials(tree, is_root=rel in allow.DB_HANDLE_PACKAGES[package])
