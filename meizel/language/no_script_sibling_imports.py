import ast
import re
import sys
from pathlib import Path

from meizel import allowances, lint_common

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PATH_IMPORT_SCOPE = allowances.load(_REPO_ROOT).names("path-imports", "scope")

# Directories that are never scanned (vendored / generated / scratch).
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

# Test files are pytest-managed (pytest inserts their directory into
# sys.path), never script-mode.
_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.py$"),
)

# A python shebang (`#!/usr/bin/env python3`) or a PEP 723 uv script
# (`#!/usr/bin/env -S uv run --script`) runs the file as a script.
_SHEBANG_RE = re.compile(r"^#!.*\b(?:python\d*(?:\.\d+)?|uv run)\b")


def _is_script_mode(tree: ast.Module, src: str) -> bool:
    """True when the file runs as a script (main block or python/uv shebang)."""
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


def _is_sys_path_guard(stmt: ast.stmt) -> bool:
    """True when stmt is a `sys.path.insert(...)` / `sys.path.append(...)` call."""
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


def _type_checking_ids(tree: ast.Module) -> set[int]:
    """ids of every node inside an `if TYPE_CHECKING:` block (never executed)."""
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
# Whether a sibling module exists as a `.py` file or package `__init__.py` in `d`.
def _sibling_kind(d: Path, first: str, *, dotted: bool) -> str | None:
    if lint_common.read_utf8_text(d / f"{first}.py") is not None:
        return None if dotted else "file"
    if lint_common.read_utf8_text(d / first / "__init__.py") is not None:
        return "pkg"
    return None


def _sibling_targets(stmt: ast.Import | ast.ImportFrom, d: Path) -> list[tuple[str, str]]:
    """Same-dir modules the statement could resolve to: (first_component, kind).

    kind = "file" | "pkg". `from sibling.sub import X` with a sibling FILE is
    excluded — a file cannot be a package, so the import resolves elsewhere.
    """
    out: list[tuple[str, str]] = []
    names = (
        [alias.name for alias in stmt.names]
        if isinstance(stmt, ast.Import)
        else ([stmt.module] if stmt.module is not None else [])
    )
    for module_name in names:
        first = module_name.split(".")[0]
        kind = _sibling_kind(d, first, dotted="." in module_name)
        if kind is not None:
            out.append((first, kind))
    return out


def _nested_blocks(stmt: ast.stmt) -> list[list[ast.stmt]] | None:
    """The statement lists nested in a compound statement, or None for any other statement."""
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


def scan_file(path: Path, *, root: Path | None = None) -> list[str]:
    """Return one error string per unguarded sibling import in a script-mode file."""
    repo_root = _REPO_ROOT if root is None else root
    src = lint_common.read_utf8_text(path)
    if src is None:
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    if not _is_script_mode(tree, src):
        return []
    try:
        rel = path.relative_to(repo_root).as_posix()
    except ValueError:
        # Path not under repo — pre-commit usually passes absolute paths so this
        # is rare; safety net so an out-of-tree target cannot crash the run.
        rel = path.as_posix()
    d = path.parent
    type_checking = _type_checking_ids(tree)
    violations: list[str] = []
    top_guards: list[int] = []

    def check_import(
        stmt: ast.Import | ast.ImportFrom, fn_guards: list[int], *, in_fn: bool
    ) -> None:
        if (isinstance(stmt, ast.ImportFrom) and stmt.level > 0) or id(stmt) in type_checking:
            return
        for first, _kind in _sibling_targets(stmt, d):
            if in_fn:
                guarded = bool(top_guards) or any(g < stmt.lineno for g in fn_guards)
            else:
                guarded = any(g < stmt.lineno for g in top_guards)
            if not guarded:
                violations.append(
                    f"{rel}:{stmt.lineno}: sibling import `{first}` in a script-mode "
                    "file — PYTHONSAFEPATH=1 keeps the script's own directory off "
                    f"sys.path. {_fix_hint(rel)}"
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
    return violations


def _fix_hint(rel: str) -> str:
    for prefix in _PATH_IMPORT_SCOPE:
        if rel.startswith(prefix):
            return (
                f"Under {prefix} a sys.path guard is itself refused (structure Rule 6): "
                "move the shared code into a governed package and import it normally."
            )
    return (
        "Add a guard before it: "
        "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: PTH100, PTH120"
    )


def _excluded(parts: tuple[str, ...]) -> bool:
    """Whether a path BELOW the scanned root runs through a vendored/generated directory.

    Only the part of the path under the root counts: a checkout that itself lives in a
    directory named `tmp` or `build` (a CI scratch dir, a pytest temp dir) must still be scanned.
    """
    return any(part in _EXCLUDED_DIRS for part in parts)


# meiva:note
# Every `.py` under the targets, or just the changed files a full walk would reach.
# The walk skips vendored/generated directories; the changed-files path applies the same
# rule without walking them.
def _files_to_scan(
    resolved: list[str],
    targets: list[Path],
    scope: frozenset[str] | None,
    *,
    root: Path = _REPO_ROOT,
) -> list[Path]:
    if targets:
        return [
            path
            for entry in resolved
            if entry.endswith(".py")
            and (path := root / entry)
            and not any(
                _excluded(path.relative_to(t).parts) for t in targets if path.is_relative_to(t)
            )
        ]
    if scope is None:
        return [p for p in root.rglob("*.py") if not _excluded(p.relative_to(root).parts)]
    return [root / rel for rel in scope if rel.endswith(".py") and not _excluded(Path(rel).parts)]


def main(argv: list[str] | None = None, *, repo_root: Path = _REPO_ROOT) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    argv, only = lint_common.split_only(argv)
    resolved: list[str] = []
    if argv:
        resolved, missing = lint_common.resolve_targets(argv, repo_root)
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
    targets = [Path(a).resolve() for a in argv]
    total = 0
    for path in sorted(
        _files_to_scan(
            resolved, targets, lint_common.changed_scope(only, repo_root), root=repo_root
        )
    ):
        rel = (
            path.relative_to(repo_root).as_posix()
            if path.is_relative_to(repo_root)
            else path.as_posix()
        )
        if any(p.search(rel) for p in _TEST_PATTERNS):
            continue
        for violation in scan_file(path, root=repo_root):
            total += 1
            print(violation)
    if total:
        print(
            f"\n{total} violation(s). See the header of meizel/language/no_script_sibling_imports.py "
            "for the guard pattern.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
