# meiva:note
# Hints about branching on a finite domain in production code (`wildcard-case`): a
# `case _:` that does not fail, and a `str`-typed selector parameter (action, status, role,
# ...) compared with string literals in a function that never raises. Each sends an
# unknown value down the last branch, and pyright's exhaustiveness check does not see
# through either. A field compared with a literal (`fact.role == "shell"`) is left to
# review: syntax cannot tell a closed vocabulary from a wire value.
from __future__ import annotations

import ast

from scripts.lint.diagnostics.diff_hints.model import FileKind, FunctionNode, HintId, Site, Unit
from scripts.lint.diagnostics.diff_hints.names import annotation_names
from scripts.lint.diagnostics.diff_hints.walk import own_nodes, parameters

_SELECTORS = frozenset({"action", "status", "role", "kind", "mode", "op", "verb"})
_LITERAL_OPS = (ast.Eq, ast.NotEq, ast.In, ast.NotIn)


def _fails(statements: list[ast.stmt]) -> bool:
    for stmt in statements:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Raise):
                return True
            if isinstance(node, ast.Call):
                callee = node.func
                name = callee.id if isinstance(callee, ast.Name) else None
                if name == "assert_never" or (
                    isinstance(callee, ast.Attribute) and callee.attr == "assert_never"
                ):
                    return True
    return False


def _wildcard_cases(unit: Unit) -> list[Site]:
    sites: list[Site] = []
    for node in unit.touched:
        if not isinstance(node, ast.match_case):
            continue
        pattern = node.pattern
        wildcard = (
            isinstance(pattern, ast.MatchAs) and pattern.pattern is None and pattern.name is None
        )
        if wildcard and not _fails(node.body):
            detail = "`case _:` neither raises nor calls `assert_never`"
            sites.append(Site(HintId.WILDCARD_CASE, unit.path, pattern.lineno, detail))
    return sites


def _is_str_literal(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.Tuple | ast.Set | ast.List):
        return bool(node.elts) and all(_is_str_literal(element) for element in node.elts)
    return False


def _selector_parameters(function: FunctionNode) -> frozenset[str]:
    selectors: set[str] = set()
    for arg, _ in parameters(function):
        if arg.arg in _SELECTORS and annotation_names(arg.annotation) - {"None"} == frozenset(
            {"str"}
        ):
            selectors.add(arg.arg)
    return frozenset(selectors)


def _selector_compare(node: ast.AST, selectors: frozenset[str]) -> str | None:
    if not (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)):
        return None
    if node.left.id not in selectors or not isinstance(node.ops[0], _LITERAL_OPS):
        return None
    return node.left.id if all(_is_str_literal(c) for c in node.comparators) else None


def _selector_sites(function: FunctionNode, unit: Unit) -> list[Site]:
    selectors = _selector_parameters(function)
    if not selectors or _fails(function.body):
        return []
    first: dict[str, int] = {}
    for node in own_nodes(function):
        name = _selector_compare(node, selectors)
        line = getattr(node, "lineno", 0)
        if name is not None and line in unit.added:
            first[name] = min(line, first.get(name, line))
    return [
        Site(
            HintId.WILDCARD_CASE,
            unit.path,
            line,
            f"`{name}: str` is compared with literals in a function that never raises",
        )
        for name, line in first.items()
    ]


def wildcard_case(unit: Unit) -> list[Site]:
    if unit.kind is not FileKind.PRODUCTION:
        return []
    sites = _wildcard_cases(unit)
    for function in unit.functions:
        sites.extend(_selector_sites(function, unit))
    return sites
