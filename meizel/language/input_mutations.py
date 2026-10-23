from __future__ import annotations

import ast
from dataclasses import dataclass

from meizel.graph.imports import Scope

__all__ = ["InputListAppend", "input_list_appends"]


@dataclass(frozen=True, slots=True)
class InputListAppend:
    line: int
    function: str
    parameter: str


def input_list_appends(
    node: ast.FunctionDef | ast.AsyncFunctionDef, scope: Scope, *, builtin_list: bool
) -> list[InputListAppend]:
    if not builtin_list or node.type_params:
        return []
    parameters = {
        arg.arg
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        if _list_annotation(arg.annotation) and arg.arg not in scope.shadowed | scope.imports.keys()
    }
    nodes: list[ast.AST] = list(node.body)
    calls: list[ast.Call] = []
    while nodes:
        current = nodes.pop()
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        if isinstance(current, ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp):
            continue
        if (binding := _local_binding(current)) is not None:
            parameters.discard(binding)
        if isinstance(current, ast.Call):
            calls.append(current)
        nodes.extend(ast.iter_child_nodes(current))
    return sorted(
        [
            InputListAppend(call.lineno, node.name, parameter)
            for call in calls
            if (parameter := _append_parameter(call, parameters)) is not None
        ],
        key=lambda item: item.line,
    )


def _list_annotation(annotation: ast.expr | None) -> bool:
    base = annotation.value if isinstance(annotation, ast.Subscript) else annotation
    return isinstance(base, ast.Name) and base.id == "list"


def _append_parameter(call: ast.Call, parameters: set[str]) -> str | None:
    func = call.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "append"
        and isinstance(func.value, ast.Name)
        and func.value.id in parameters
    ):
        return func.value.id
    return None


def _local_binding(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
        return node.id
    if isinstance(node, ast.MatchAs | ast.MatchStar):
        return node.name
    if isinstance(node, ast.MatchMapping):
        return node.rest
    return None
