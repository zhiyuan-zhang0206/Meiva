# meiva:note
# Which files the hints judge. Production hints govern the application and template
# packages, test hints the tests of those packages and the top-level test directory;
# tooling, examples and the frontend are outside both. Declared ambient kinds arrive
# with each unit; the filename heuristic remains for legacy composition roots.
from __future__ import annotations

import ast
from bisect import bisect_left

from meizel.graph.model import KindName
from scripts.lint.diagnostics.diff_hints.model import (
    FileKind,
    FunctionNode,
    Importers,
    Roles,
    Unit,
)
from scripts.lint.diagnostics.diff_hints.names import import_names

_ROOT_FILE_NAMES = ("main.py", "__main__.py", "runtime.py", "app.py")


def is_test_path(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    if "tests" in parts[:-1]:
        return True
    return name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py")


def is_root_file(path: str) -> bool:
    return path.rsplit("/", 1)[-1] in _ROOT_FILE_NAMES


def classify(path: str, roles: Roles) -> FileKind:
    if not path.endswith(".py"):
        return FileKind.OTHER
    top = path.split("/", 1)[0]
    if is_test_path(path):
        return FileKind.TEST if top in roles.production | roles.tests else FileKind.OTHER
    return FileKind.PRODUCTION if top in roles.production else FileKind.OTHER


def _own_line(node: ast.AST) -> int | None:
    if isinstance(node, ast.match_case):
        return node.pattern.lineno
    if isinstance(
        node,
        ast.stmt
        | ast.expr
        | ast.excepthandler
        | ast.arg
        | ast.keyword
        | ast.alias
        | ast.pattern
        | ast.type_param,
    ):
        return node.lineno
    return None


def _holds(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Call, lines: list[int]
) -> bool:
    decorators = [] if isinstance(node, ast.Call) else node.decorator_list
    start = min([node.lineno, *(d.lineno for d in decorators)])
    end = node.end_lineno or start
    at = bisect_left(lines, start)
    return at < len(lines) and lines[at] <= end


# meiva:note
# The parsed unit of a file, or None when the file is out of scope, the change added no
# line, or the file does not parse (a syntax error is another gate's finding, not a hint).
def parse_unit(
    path: str,
    text: str,
    added: frozenset[int],
    roles: Roles,
    roots: frozenset[KindName],
    importers: Importers,
) -> Unit | None:
    kind = classify(path, roles)
    if kind is FileKind.OTHER or not added:
        return None
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    nodes = list(ast.walk(tree))
    lines = sorted(added)
    functions: list[FunctionNode] = []
    classes: list[ast.ClassDef] = []
    calls: list[ast.Call] = []
    for node in nodes:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _holds(node, lines):
            functions.append(node)
        elif isinstance(node, ast.ClassDef) and _holds(node, lines):
            classes.append(node)
        elif isinstance(node, ast.Call) and _holds(node, lines):
            calls.append(node)
    touched = tuple(node for node in nodes if _own_line(node) in added)
    names = import_names(nodes)
    return Unit(
        path,
        tree,
        added,
        kind,
        roles,
        roots,
        importers,
        names,
        touched,
        tuple(functions),
        tuple(classes),
        tuple(calls),
    )
