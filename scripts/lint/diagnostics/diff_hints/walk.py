# meiva:note
# Tree walking shared by the detectors: the nodes that belong to one function, and a
# function's parameters paired with their defaults.
from __future__ import annotations

import ast
from collections.abc import Iterator

from scripts.lint.diagnostics.diff_hints.model import FunctionNode


# meiva:note
# Every node of a function body that belongs to that function: nested functions,
# lambdas and classes are other scopes and are not entered.
def own_nodes(function: FunctionNode) -> Iterator[ast.AST]:
    return scope_nodes(function.body)


# meiva:note
# The same walk over any statement list, so module-level code is a scope too.
def scope_nodes(body: list[ast.stmt]) -> Iterator[ast.AST]:
    pending: list[ast.AST] = list(body)
    while pending:
        node = pending.pop()
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        pending.extend(ast.iter_child_nodes(node))


def parameters(function: FunctionNode) -> list[tuple[ast.arg, ast.expr | None]]:
    args = function.args
    positional = [*args.posonlyargs, *args.args]
    offset = len(positional) - len(args.defaults)
    pairs: list[tuple[ast.arg, ast.expr | None]] = [
        (arg, args.defaults[index - offset] if index >= offset else None)
        for index, arg in enumerate(positional)
    ]
    pairs.extend(zip(args.kwonlyargs, args.kw_defaults, strict=True))
    return pairs
