from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from typing import Literal

__all__ = ["SiblingKind", "excluded", "judged", "violations"]

# meiva:note
# What a same-directory name can be: a module file (`helper.py`) or a package (`helper/__init__.py`).
# The caller reads the directory; a decision here never touches the disk.
SiblingKind = Literal["file", "pkg"]

# meiva:note
# Directories that are never scanned (vendored / generated / scratch). The entry that walks the
# tree applies this to the part of a path below its scan root, so a checkout inside a `tmp` or
# `build` directory is still scanned.
_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".next",
        "dist",
        "build",
        "outputs",
        "runs",
        "logs",
        "tmp",
        ".worktrees",
        ".claude",
        "site-packages",
    }
)

# meiva:note
# Test files are pytest-managed (pytest inserts their directory into sys.path), never script-mode.
_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.py$"),
)

# meiva:note
# A python shebang (`#!/usr/bin/env python3`) or a PEP 723 uv script
# (`#!/usr/bin/env -S uv run --script`) runs the file as a script.
_SHEBANG_RE = re.compile(r"^#!.*\b(?:python\d*(?:\.\d+)?|uv run)\b")

_GUARD_HINT = (
    "Add a guard before it: "
    "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: PTH100, PTH120"
)


# meiva:note
# Whether a path BELOW the scanned root runs through a vendored or generated directory.
def excluded(parts: tuple[str, ...]) -> bool:
    return any(part in _EXCLUDED_DIRS for part in parts)


# meiva:note
# Whether this rule judges the file at all: pytest owns its own path insertion, so a test file is
# never script-mode, whatever its shebang or main block says.
def judged(rel: str) -> bool:
    return not any(pattern.search(rel) for pattern in _TEST_PATTERNS)


# meiva:note
# One error string per unguarded sibling import. `siblings` is what the file's own directory holds,
# by module name: the caller reads it, so the same source is judged differently against a directory
# that holds the module and one that does not.
def violations(
    rel: str,
    source: str,
    siblings: Mapping[str, SiblingKind],
    *,
    scope: tuple[str, ...] = (),
) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    if not _is_script_mode(tree, source):
        return []
    type_checking = _type_checking_ids(tree)
    found: list[str] = []
    top_guards: list[int] = []

    def check_import(
        stmt: ast.Import | ast.ImportFrom, fn_guards: list[int], *, in_fn: bool
    ) -> None:
        if (isinstance(stmt, ast.ImportFrom) and stmt.level > 0) or id(stmt) in type_checking:
            return
        for first in _sibling_targets(stmt, siblings):
            if in_fn:
                guarded = bool(top_guards) or any(g < stmt.lineno for g in fn_guards)
            else:
                guarded = any(g < stmt.lineno for g in top_guards)
            if not guarded:
                found.append(
                    f"{rel}:{stmt.lineno}: sibling import `{first}` in a script-mode "
                    "file — PYTHONSAFEPATH=1 keeps the script's own directory off "
                    f"sys.path. {_fix_hint(rel, scope)}"
                )

    def walk(stmts: list[ast.stmt], fn_guards: list[int], *, in_fn: bool) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(stmt.body, [], in_fn=True)
            elif isinstance(stmt, ast.ClassDef):
                walk(stmt.body, fn_guards, in_fn=in_fn)
            elif _is_sys_path_guard(stmt):
                (fn_guards if in_fn else top_guards).append(stmt.lineno)
            elif (blocks := _nested_blocks(stmt)) is not None:
                for block in blocks:
                    walk(block, fn_guards, in_fn=in_fn)
            elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
                check_import(stmt, fn_guards, in_fn=in_fn)

    walk(tree.body, [], in_fn=False)
    return found


# meiva:note
# True when the file runs as a script (main block or python/uv shebang).
def _is_script_mode(tree: ast.Module, src: str) -> bool:
    first_line = src.splitlines()[0] if src else ""
    if _SHEBANG_RE.search(first_line):
        return True
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
        ):
            for op, cmp in zip(node.test.ops, node.test.comparators, strict=False):
                if (
                    isinstance(op, ast.Eq)
                    and isinstance(cmp, ast.Constant)
                    and cmp.value == "__main__"
                ):
                    return True
    return False


# meiva:note
# True when stmt is a `sys.path.insert(...)` / `sys.path.append(...)` call.
def _is_sys_path_guard(stmt: ast.stmt) -> bool:
    if not isinstance(stmt, ast.Expr):
        return False
    call = stmt.value
    if not isinstance(call, ast.Call):
        return False
    fn = call.func
    return (
        isinstance(fn, ast.Attribute)
        and fn.attr in ("insert", "append")
        and isinstance(fn.value, ast.Attribute)
        and fn.value.attr == "path"
        and isinstance(fn.value.value, ast.Name)
        and fn.value.value.id == "sys"
    )


# meiva:note
# ids of every node inside an `if TYPE_CHECKING:` block (never executed).
def _type_checking_ids(tree: ast.Module) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            for child in ast.walk(node):
                ids.add(id(child))
    return ids


# meiva:note
# Same-directory modules a statement could resolve to. `from sibling.sub import X` with a sibling
# FILE is excluded — a file cannot be a package, so the import resolves elsewhere.
def _sibling_targets(
    stmt: ast.Import | ast.ImportFrom, siblings: Mapping[str, SiblingKind]
) -> list[str]:
    out: list[str] = []
    names = (
        [alias.name for alias in stmt.names]
        if isinstance(stmt, ast.Import)
        else ([stmt.module] if stmt.module is not None else [])
    )
    for module_name in names:
        first, _, below = module_name.partition(".")
        kind = siblings.get(first)
        if kind is not None and not (kind == "file" and below):
            out.append(first)
    return out


# meiva:note
# The statement lists nested in a compound statement, or None for any other statement.
def _nested_blocks(stmt: ast.stmt) -> list[list[ast.stmt]] | None:
    if isinstance(stmt, (ast.If, ast.For, ast.While)):
        return [stmt.body, stmt.orelse]
    if isinstance(stmt, ast.Try):
        return [
            stmt.body,
            *(handler.body for handler in stmt.handlers),
            stmt.orelse,
            stmt.finalbody,
        ]
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return [stmt.body]
    return None


# meiva:note
# The repair the finding names: under a scope where a sys.path guard is itself refused (structure
# Rule 6) the hint is the package, never a guard the gate would reject.
def _fix_hint(rel: str, scope: tuple[str, ...]) -> str:
    for prefix in scope:
        if rel.startswith(prefix):
            return (
                f"Under {prefix} a sys.path guard is itself refused (structure Rule 6): "
                "move the shared code into a governed package and import it normally."
            )
    return _GUARD_HINT
