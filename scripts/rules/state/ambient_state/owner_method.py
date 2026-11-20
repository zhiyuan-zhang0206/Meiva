# meiva:note
# Executable method syntax shared by Thread and Task ownership evidence.
#
# These facts exclude uninvoked nested definitions and constant-dead branches.
# They identify direct instance fields, assignments, calls and visible join timeout
# syntax; they do not prove control-flow ordering or a finite runtime deadline.
# Lifecycle acceptance belongs to each Thread/Task proof owner.

from __future__ import annotations

import ast
import math
from collections.abc import Iterator

from scripts.rules.state.ambient_state.module import dotted

__all__ = [
    "Function",
    "assignment_pairs",
    "call_nodes",
    "executable_nodes",
    "instance_field",
    "join_has_timeout",
]

Function = ast.FunctionDef | ast.AsyncFunctionDef


# meiva:note
# Walk executable statements without treating nested definitions as executed.
def executable_nodes(root: ast.AST) -> Iterator[ast.AST]:
    pending: list[ast.AST] = (
        [root] if isinstance(root, ast.Call) else list(ast.iter_child_nodes(root))
    )
    while pending:
        node = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        yield node
        if isinstance(node, ast.If) and isinstance(node.test, ast.Constant):
            branch = node.body if node.test.value else node.orelse
            pending.extend([node.test, *branch])
        else:
            pending.extend(ast.iter_child_nodes(node))


# meiva:note
# Name of a direct self field; nested or computed receivers stay unknown.
def instance_field(node: ast.AST) -> str | None:
    name = dotted(node)
    return name if name and name.startswith("self.") and name.count(".") == 1 else None


# meiva:note
# Visible plain/annotated assignment pairs, retaining the original AST nodes.
def assignment_pairs(root: ast.AST) -> Iterator[tuple[ast.expr, ast.expr]]:
    for node in executable_nodes(root):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                yield target, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            yield node.target, node.value


# meiva:note
# Visible calls, including root when it is itself a call.
def call_nodes(root: ast.AST) -> Iterator[ast.Call]:
    return (node for node in executable_nodes(root) if isinstance(node, ast.Call))


# meiva:note
# Recognize timeout syntax, rejecting absent/invalid literal budgets.
#
#     The caller identifies the join operation and its timeout argument layout.
#     A dynamic expression remains structural evidence, not a runtime bound.
def join_has_timeout(call: ast.Call) -> bool:
    timeout = next((kw.value for kw in call.keywords if kw.arg == "timeout"), None)
    timeout = timeout if timeout is not None else (call.args[0] if call.args else None)
    if timeout is None:
        return False
    if isinstance(timeout, ast.Constant):
        return (
            isinstance(timeout.value, int | float)
            and not isinstance(timeout.value, bool)
            and math.isfinite(timeout.value)
        )
    return not (isinstance(timeout, ast.Call) and dotted(timeout.func) == "float")
