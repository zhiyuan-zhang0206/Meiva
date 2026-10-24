# meiva:note
# Hints about what a function signature promises: a callable parameter with a default
# (`callable-default`), a `None` default that falls back to the real implementation
# (`none-fallback`), an `Any` or `object` collaborator (`any-collaborator`, judged by the
# parameter's name: a client, a pool, a config; a `value: object` that a parser validates
# is data) and a
# `Protocol` defined above `base/` (`protocol-outside-base`), and a lambda passed for a
# callback parameter (`lambda-dependency`). Only lines the change added can fire.
from __future__ import annotations

import ast

from scripts.lint.diagnostics.diff_hints.model import FileKind, FunctionNode, HintId, Site, Unit
from scripts.lint.diagnostics.diff_hints.names import (
    OS_ENVIRON,
    annotation_names,
    dotted,
    type_head,
    union_members,
)
from scripts.lint.diagnostics.diff_hints.walk import own_nodes, parameters

_CALLABLE_TYPES = frozenset({"Callable", "Coroutine", "Awaitable"})
_CALLBACK_SUFFIXES = ("_fn", "_hook", "_cb", "_reader", "_factory", "_fetch")
_OPAQUE_TYPES = frozenset({"Any", "object"})
_COLLABORATOR_NAMES = frozenset(
    {
        "client",
        "clients",
        "pool",
        "conn",
        "connection",
        "session",
        "config",
        "context",
        "ctx",
        "graph",
        "service",
        "backend",
        "store",
        "db",
        "database",
        "runner",
        "producer",
        "bus",
        "host",
        "repo",
        "settings",
        "llm",
        "model",
        "pubsub",
        "handler",
        "provider",
        "manager",
        "engine",
        "transport",
        "stream",
        "registry",
        "gateway",
        "queue",
        "cache",
    }
)
_PLAIN_CONSTRUCTORS = frozenset(
    {"dict", "list", "set", "tuple", "frozenset", "str", "int", "float", "bool", "bytes", "object"}
)
_PROTOCOLS = frozenset({"typing.Protocol", "typing_extensions.Protocol"})
_SETTINGS = "base.config.settings"


def _callable_reason(arg: ast.arg) -> str | None:
    if annotation_names(arg.annotation) & _CALLABLE_TYPES:
        return "is a callable"
    if arg.arg.endswith(_CALLBACK_SUFFIXES):
        return "is named like a callback"
    return None


def _is_stub_default(default: ast.expr) -> bool:
    return isinstance(default, ast.Constant) and default.value is Ellipsis


def callable_default(unit: Unit) -> list[Site]:
    sites: list[Site] = []
    for function in unit.functions:
        for arg, default in parameters(function):
            if default is None or _is_stub_default(default) or arg.lineno not in unit.added:
                continue
            reason = _callable_reason(arg)
            if reason is not None:
                detail = f"parameter `{arg.arg}` {reason} and has a default"
                sites.append(Site(HintId.CALLABLE_DEFAULT, unit.path, arg.lineno, detail))
    return sites


def _is_none(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _none_parameters(function: FunctionNode) -> dict[str, int]:
    return {
        arg.arg: arg.lineno
        for arg, default in parameters(function)
        if default is not None and _is_none(default)
    }


def _real_value(expr: ast.expr, unit: Unit) -> bool:
    if isinstance(expr, ast.Call):
        callee = expr.func
        return not (isinstance(callee, ast.Name) and callee.id in _PLAIN_CONSTRUCTORS)
    if isinstance(expr, ast.Attribute | ast.Name):
        name = dotted(expr, unit.names)
        return name is not None and (name == OS_ENVIRON or name.startswith(_SETTINGS))
    return False


# meiva:note
# `(parameter, whether the test is true when the parameter is None)` for `p is None`,
# `p is not None`, `p == None` and `not p`; None for any other test.
def _none_test(test: ast.expr) -> tuple[str, bool] | None:
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        if isinstance(test.operand, ast.Name):
            return test.operand.id, True
        return None
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1):
        return None
    left, right = test.left, test.comparators[0]
    if not (isinstance(left, ast.Name) and _is_none(right)):
        return None
    if isinstance(test.ops[0], ast.Is | ast.Eq):
        return left.id, True
    if isinstance(test.ops[0], ast.IsNot | ast.NotEq):
        return left.id, False
    return None


def _or_fallback(node: ast.BoolOp, unit: Unit, none_params: dict[str, int]) -> list[int]:
    first = node.values[0]
    guarded = isinstance(first, ast.Name) and first.id in none_params
    if guarded and any(_real_value(value, unit) for value in node.values[1:]):
        return [node.lineno]
    return []


def _conditional_fallback(node: ast.IfExp, unit: Unit, none_params: dict[str, int]) -> list[int]:
    tested = _none_test(node.test)
    if tested is None or tested[0] not in none_params:
        return []
    none_branch = node.body if tested[1] else node.orelse
    return [node.lineno] if _real_value(none_branch, unit) else []


def _if_fallback(node: ast.If, unit: Unit, none_params: dict[str, int]) -> list[int]:
    tested = _none_test(node.test)
    if tested is None or not tested[1] or tested[0] not in none_params:
        return []
    return [
        inner.lineno
        for stmt in node.body
        for inner in ast.walk(stmt)
        if isinstance(inner, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == tested[0] for t in inner.targets)
        and _real_value(inner.value, unit)
    ]


def _fallback_sites(node: ast.AST, unit: Unit, none_params: dict[str, int]) -> list[int]:
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return _or_fallback(node, unit, none_params)
    if isinstance(node, ast.IfExp):
        return _conditional_fallback(node, unit, none_params)
    if isinstance(node, ast.If):
        return _if_fallback(node, unit, none_params)
    return []


def none_fallback(unit: Unit) -> list[Site]:
    if unit.kind is not FileKind.PRODUCTION:
        return []
    sites: list[Site] = []
    for function in unit.functions:
        none_params = _none_parameters(function)
        if not none_params:
            continue
        param_added = any(line in unit.added for line in none_params.values())
        for node in own_nodes(function):
            for line in _fallback_sites(node, unit, none_params):
                if line in unit.added or param_added:
                    detail = "a `None` default falls back to a real implementation"
                    sites.append(Site(HintId.NONE_FALLBACK, unit.path, line, detail))
    return sites


def _opaque_member(annotation: ast.expr | None) -> str | None:
    if annotation is None:
        return None
    for member in union_members(annotation):
        head = type_head(member)
        if head in _OPAQUE_TYPES and not isinstance(member, ast.Subscript):
            return head
    return None


def any_collaborator(unit: Unit) -> list[Site]:
    if unit.kind is not FileKind.PRODUCTION:
        return []
    sites: list[Site] = []
    for function in unit.functions:
        if function.name.startswith("__") and function.name.endswith("__"):
            continue
        for arg, _ in parameters(function):
            opaque = _opaque_member(arg.annotation)
            named_like_data = arg.arg not in _COLLABORATOR_NAMES
            if opaque is not None and not named_like_data and arg.lineno in unit.added:
                detail = f"parameter `{arg.arg}` is typed `{opaque}`"
                sites.append(Site(HintId.ANY_COLLABORATOR, unit.path, arg.lineno, detail))
    return sites


def protocol_outside_base(unit: Unit) -> list[Site]:
    if unit.path.startswith("base/"):
        return []
    sites: list[Site] = []
    for node in unit.classes:
        if node.lineno not in unit.added:
            continue
        for base in node.bases:
            head = base.value if isinstance(base, ast.Subscript) else base
            if dotted(head, unit.names) in _PROTOCOLS:
                detail = f"`{node.name}` is a Protocol defined outside base/"
                sites.append(Site(HintId.PROTOCOL_OUTSIDE_BASE, unit.path, node.lineno, detail))
    return sites


_CALLBACK_KEYWORDS = ("_reader", "_fn", "_hook")
_VALUE_WRAPPERS = (ast.Name, ast.Attribute, ast.Constant)


def _wraps_a_value(keyword: ast.keyword) -> bool:
    name = keyword.arg or ""
    lam = keyword.value
    if not isinstance(lam, ast.Lambda):
        return False
    if name.endswith(_CALLBACK_KEYWORDS):
        return True
    return name.endswith("_factory") and not lam.args.args and isinstance(lam.body, _VALUE_WRAPPERS)


def lambda_dependency(unit: Unit) -> list[Site]:
    sites: list[Site] = []
    for node in unit.calls:
        for keyword in node.keywords:
            if _wraps_a_value(keyword) and keyword.value.lineno in unit.added:
                detail = f"`{keyword.arg}=lambda ...` wraps a value back into a callback"
                sites.append(
                    Site(HintId.LAMBDA_DEPENDENCY, unit.path, keyword.value.lineno, detail)
                )
    return sites
