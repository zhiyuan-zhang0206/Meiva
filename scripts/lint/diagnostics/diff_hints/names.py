# meiva:note
# Name resolution shared by the detectors. A detector asks "which dotted name is this
# expression" through the file's own imports, so `from time import monotonic as tick`
# and `import time as t` both resolve to `time.monotonic`.
from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping

# meiva:note
# The process environment name shared by default-collaborator and test-seam detectors.
OS_ENVIRON = "os.environ"


def import_names(nodes: Iterable[ast.AST]) -> dict[str, str]:
    names: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                names[alias.asname or top] = alias.name if alias.asname else top
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


# meiva:note
# The dotted name of a Name or Attribute chain with its first segment resolved
# through the file's imports, or None when the chain does not start at an imported
# name (a local variable, a call result, a subscript).
def dotted(node: ast.expr, names: Mapping[str, str]) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    origin = names.get(current.id)
    if origin is None:
        return None
    return ".".join([origin, *reversed(parts)])


# meiva:note
# Every plain name an annotation mentions, including the members of a union and the
# contents of a quoted annotation.
def annotation_names(annotation: ast.expr | None) -> frozenset[str]:
    if annotation is None:
        return frozenset()
    found: set[str] = set()
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found |= _quoted_names(node.value)
    return frozenset(found)


def _quoted_names(text: str) -> frozenset[str]:
    try:
        parsed = ast.parse(text, mode="eval").body
    except SyntaxError:
        return frozenset()
    return annotation_names(parsed)


def decorator_name(decorator: ast.expr, names: Mapping[str, str]) -> str | None:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return dotted(target, names)


# meiva:note
# The members of a top-level union annotation (`A | B`, `Optional[A]`, `Union[A, B]`,
# or a quoted one); any other annotation is its own single member.
def union_members(annotation: ast.expr) -> list[ast.expr]:
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            return union_members(ast.parse(annotation.value, mode="eval").body)
        except SyntaxError:
            return [annotation]
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return [*union_members(annotation.left), *union_members(annotation.right)]
    if isinstance(annotation, ast.Subscript) and type_head(annotation) in ("Optional", "Union"):
        inner = annotation.slice
        members = inner.elts if isinstance(inner, ast.Tuple) else [inner]
        return [leaf for member in members for leaf in union_members(member)]
    return [annotation]


# meiva:note
# The class an annotation names before any subscript: `list[str]` is `list`,
# `typing.Callable[..., int]` is `Callable`.
def type_head(annotation: ast.expr) -> str | None:
    if isinstance(annotation, ast.Subscript):
        return type_head(annotation.value)
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    return None
