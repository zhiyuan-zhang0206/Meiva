# meiva:note
# Which files can see a change: the reverse import closure of the modules it edits. It is the one
# question a red type check asks before it can say whose error it is -- a signature change breaks
# its *callers*, which are by definition files the change never touched (#7910's seven sites were
# all in a test file it never edited) -- and the answer is a set of paths, so the runner can keep
# this scan beside the scheduling logic instead of inside it.

from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path

# meiva:note
# Directories whose Python is not part of this checkout's import graph: a generated build tree and
# another checkout's worktree both hold modules that resolve to the same names as real ones.
_SKIP_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__", ".worktrees", ".next"})


def _module_name(path: Path, root: Path) -> str | None:
    relative: Path | None = None
    for candidate, anchor in ((path, root), (path.resolve(), root.resolve())):
        try:
            relative = candidate.relative_to(anchor)
            break
        except ValueError:
            continue
    if relative is None:
        return None
    if relative.suffix != ".py":
        return None
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imported_modules(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _changed_modules(root: Path, files: Sequence[str]) -> set[str]:
    return {
        name
        for path in files
        if path.endswith(".py") and (name := _module_name(root / path, root)) is not None
    }


def _imports_changed(imported: str, changed: set[str]) -> bool:
    return imported in changed or any(imported.startswith(f"{module}.") for module in changed)


def _importers(root: Path, changed: set[str]) -> dict[str, set[str]] | None:
    importers: dict[str, set[str]] = {}
    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text())
        except (OSError, SyntaxError, UnicodeDecodeError):
            return None
        name = _module_name(path, root)
        if name is None:
            continue
        imported = _imported_modules(tree)
        if any(_imports_changed(one, changed) for one in imported):
            importers[name] = imported
    return importers


def _reachable(importers: dict[str, set[str]]) -> set[str]:
    reached = set(importers)
    frontier = set(importers)
    while frontier:
        frontier = {
            module
            for module, imported in importers.items()
            if module not in reached and imported & frontier
        }
        reached |= frontier
    return reached


# meiva:note
# The reverse-reachability set of this change, or None when a file could not be read: a tool that
# cannot prove the errors are outside the change must not report them as outside it.
def closure(root: Path, files: Sequence[str]) -> set[str] | None:
    changed = _changed_modules(root, files)
    if not changed:
        return set()
    importers = _importers(root, changed)
    if importers is None:
        return None
    return {name.replace(".", "/") + ".py" for name in _reachable(importers)}
