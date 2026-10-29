"""Function quality metrics and strict budgets for the structure gate."""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from radon.visitors import Class, Function

COMPLEXITY_HARD = 15
NESTING_CEILING = 5
QUALITY_SECTIONS = ("complexity", "nesting")
CONTROL = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.Match)
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def function_nodes(tree: ast.AST, path: str) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Index functions by Python qualname, numbering redefinitions by source order."""
    found: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        if isinstance(node, FUNCTIONS):
            name = prefix + node.name
            found.append((name, node))
            prefix = name + ".<locals>."
        elif isinstance(node, ast.ClassDef):
            prefix += node.name + "."
        for child in ast.iter_child_nodes(node):
            # Definitions can occur only in statement bodies, never expressions.
            if isinstance(child, (ast.stmt, ast.ExceptHandler, ast.match_case)):
                visit(child, prefix)

    visit(tree, "")
    counts: Counter[str] = Counter()
    result: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for name, node in sorted(found, key=lambda item: item[1].lineno):
        counts[name] += 1
        suffix = f"#{counts[name]}" if counts[name] > 1 else ""
        result[f"{path}::{name}{suffix}"] = node
    return result


def _block_scores(blocks: list[Class | Function]) -> dict[tuple[int, str], int]:
    """Include methods, closures and inner classes without double-counting methods."""
    result: dict[tuple[int, str], int] = {}
    for block in blocks:
        if isinstance(block, Class):
            result.update(_block_scores([*block.methods, *block.inner_classes]))
        else:
            result[block.lineno, block.name] = block.complexity
            result.update(_block_scores(block.closures))
    return result


def complexity_scores(
    tree: ast.AST,
    nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    *,
    visitor_from_ast: Callable[[ast.AST], Any],
) -> dict[str, int]:
    """Use Radon 6.0.1 on the shared AST, including its local-class collection gap."""
    scores = _block_scores(visitor_from_ast(tree).blocks)
    result: dict[str, int] = {}
    for key, node in nodes.items():
        identity = (node.lineno, node.name)
        if identity not in scores:
            # Radon discards classes inside functions (and their methods/closures).
            # Reuse the existing node and the same visitor, never reparse source.
            scores.update(_block_scores(visitor_from_ast(node).blocks))
        result[key] = scores[identity]
    return result


def _if_depth(node: ast.If, depth: int) -> int:
    current = depth + 1
    deepest = max((_node_depth(child, current) for child in node.body), default=current)
    # Match the audit's AST shape: elif keeps its parent's depth; else: if deepens.
    if (
        len(node.orelse) == 1
        and isinstance(node.orelse[0], ast.If)
        and node.orelse[0].col_offset == node.col_offset
    ):
        return max(deepest, _node_depth(node.orelse[0], depth))
    return max(deepest, *(_node_depth(child, current) for child in node.orelse), current)


def _node_depth(node: ast.AST, depth: int) -> int:
    if isinstance(node, (*FUNCTIONS, ast.ClassDef, ast.Lambda)):
        return depth
    if isinstance(node, ast.If):
        return _if_depth(node, depth)
    if isinstance(node, CONTROL):
        depth += 1
    if isinstance(node, ast.Match):
        children = [child for case in node.cases for child in case.body]
    else:
        # Includes Try handlers/else/finally at the control node's incremented depth.
        children = (
            child
            for child in ast.iter_child_nodes(node)
            if isinstance(child, (ast.stmt, ast.ExceptHandler))
        )
    return max((_node_depth(child, depth) for child in children), default=depth)


def max_depth(body: list[ast.stmt]) -> int:
    """Port of audit 6562: deepest control-flow path, with nested definitions separate."""
    return max((_node_depth(node, 0) for node in body), default=0)


def measure_quality(
    tree: ast.AST,
    path: str,
    *,
    visitor_from_ast: Callable[[ast.AST], Any],
) -> dict[str, dict[str, int]]:
    nodes = function_nodes(tree, path)
    return {
        "complexity": complexity_scores(tree, nodes, visitor_from_ast=visitor_from_ast),
        "nesting": {key: max_depth(node.body) for key, node in nodes.items()},
    }


# meiva:note
# The document that states the ceilings; every message of this owner points here.
_DOC = "docs/conventions/python-conventions.md (function quality budgets)"


# meiva:note
# One quality ceiling: what it counts, why it exists, and the repair a violation asks for. The
# message prints all four, so a new ceiling cannot be added without saying why it exists.
@dataclass(frozen=True)
class _Ceiling:
    kind: str
    ceiling: int
    why: str
    fix: str


# meiva:note
# Why each ceiling exists, in its own words: the number is not a style preference but the point
# where a function stops being checkable by reading, and the author who hits it now owns the split.
# Written once per kind here rather than at the reporting site.
_CEILINGS = (
    _Ceiling(
        kind="complexity",
        ceiling=COMPLEXITY_HARD - 1,
        why=(
            "every branch is one more path someone has to read and one more case a test has to "
            "cover, so past this many the function cannot be checked by reading it and the path "
            "nobody exercises is where the defect lands — whoever splits it owns that split"
        ),
        fix=(
            "separate pure decisions from I/O orchestration, replace flag or mode cascades "
            "with explicit data or focused domain helpers, and keep each function one step"
        ),
    ),
    _Ceiling(
        kind="nesting",
        ceiling=NESTING_CEILING,
        why=(
            "depth is what a reader has to hold at once: every level re-enters a scope and hides "
            "which guards already ran, so a body this deep cannot be checked one screen at a time "
            "— whoever flattens it owns the structure it becomes"
        ),
        fix=(
            "use guard clauses and early returns, extract inner loops or branch bodies into "
            "domain helpers, and separate transformations from resource and error handling"
        ),
    ),
)


def quality_errors(measurements: dict[str, dict[str, int]]) -> list[str]:
    """Every hard violation fails; no rename or frozen value grants an allowance."""
    return [
        f"{key}: {ceiling.kind} {value}: over the {ceiling.ceiling} ceiling — {ceiling.fix}; "
        f"{ceiling.why}. See {_DOC}."
        for ceiling in _CEILINGS
        for key, value in measurements[ceiling.kind].items()
        if value > ceiling.ceiling
    ]
