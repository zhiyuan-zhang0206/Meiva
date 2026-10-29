"""`debt carry --move-symbol`: what one function or class costs, so its debt can follow it.

A baseline names files, not functions, so a fixture that moves into another file leaves its entries
in the old file's keys. The move is read from the code on both sides: the violations inside the
symbol's lines in the old file at the base, and inside the same symbol in the new file.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import dataclass

from meizel.contracts.ambient_read import sources_from
from meizel.contracts.analysis import diagnose
from meizel.contracts.manifest import Manifest
from meizel.contracts.policy import load_policy
from meizel.graph.model import Code, MeizelError, Sources
from meizel.graph.universe import Universe
from meizel.language.scan import FileScan, scan_source

__all__ = [
    "SymbolMove",
    "defines",
    "parse_symbol_move_lines",
    "parse_symbol_moves",
    "retarget_imports",
    "symbol_debt",
]

type Found = dict[tuple[Code, str, str], int]


@dataclass(frozen=True, slots=True)
class SymbolMove:
    """The top-level function or class `name` moved from file `old` to file `new`."""

    old: str
    name: str
    new: str


_EXTENSIONS = (".py", ".ts", ".tsx", ".mts", ".cts")


def parse_symbol_moves(specs: list[str]) -> list[SymbolMove]:
    moves: list[SymbolMove] = []
    for spec in specs:
        head, _, new = spec.partition("=")
        old, _, name = head.partition("::")
        if (
            not (old.endswith(_EXTENSIONS) and new.endswith(_EXTENSIONS) and name.isidentifier())
            or old == new
        ):
            raise MeizelError(
                f"{spec!r}: a symbol move is written OLD_FILE::NAME=NEW_FILE with two different "
                "Python or TypeScript files and the name of a top-level function or class"
            )
        moves.append(SymbolMove(old, name, new))
    return moves


# meiva:note
# The declaration a change writes in its pull request body when it moves a symbol:
#
#   Symbol-Move: <old file>::<name> -> <new file> — <why>
#
# It is read from the same record as `Policy-Removal` — the pull request body, or for a Trunk batch the
# pull requests that batch tests — and it lives exactly as long as the change that needs it, so there
# is no table that later needs clearing.
_SYMBOL_MOVE = re.compile(
    r"^Symbol-Move:[ \t]*(?P<old>[^\s:]+\.py)::(?P<name>\w+)[ \t]*->[ \t]*(?P<new>[^\s]+\.py)"
    r"(?:[ \t]*(?:—|--)\s*.*)?$",
    re.MULTILINE,
)


def parse_symbol_move_lines(text: str) -> list[SymbolMove]:
    moves: list[SymbolMove] = []
    for match in _SYMBOL_MOVE.finditer(text):
        move = SymbolMove(match["old"], match["name"], match["new"])
        if move.old != move.new and move not in moves:
            moves.append(move)
    return moves


# meiva:note
# The base's own import lines that name a moved symbol at its old home, pointed at the new one: the
# same site, the same count, measured where the change put the symbol. Returns the source and
# `(line, old module, new module)` for each line it rewrote, so the gate can say what it changed.
def retarget_imports(
    path: str, source: str, move: SymbolMove
) -> tuple[str, list[tuple[int, str, str]]]:
    tree = _parse(path, source.encode())
    old_module = move.old.removesuffix(".py").replace("/", ".")
    new_module = move.new.removesuffix(".py").replace("/", ".")
    numbered = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and not node.level
        and node.module == old_module
        and any(alias.name == move.name for alias in node.names)
    ]
    if not numbered:
        return source, []
    text = source.splitlines(keepends=True)
    rewrites: list[tuple[int, str, str]] = []
    for number in sorted(set(numbered)):
        line = text[number - 1]
        updated = re.sub(
            rf"^(\s*from\s+){re.escape(old_module)}(\s+import\b)",
            rf"\g<1>{new_module}\g<2>",
            line,
        )
        if updated == line:
            continue
        text[number - 1] = updated
        rewrites.append((number, old_module, new_module))
    return "".join(text), rewrites


def _py_span(tree: ast.Module, name: str) -> tuple[int, int] | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and (
            node.name == name
        ):
            first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            return first, node.end_lineno or node.lineno
    return None


def _parse(path: str, source: bytes) -> ast.Module:
    try:
        return ast.parse(source, filename=path)
    except (SyntaxError, ValueError) as error:
        raise MeizelError(f"{path}: cannot parse: {error}") from error


def _source_span(scan: FileScan, source: bytes, name: str) -> tuple[int, int] | None:
    if scan.path.endswith(".py"):
        return _py_span(_parse(scan.path, source), name)
    return next(((start, end) for symbol, start, end in scan.symbols if symbol == name), None)


def defines(universe: Universe, path: str, source: bytes, name: str) -> bool:
    scan = scan_source(universe, path, source, Sources())
    return _source_span(scan, source, name) is not None


def symbol_debt(
    universe: Universe,
    manifests: dict[str, Manifest],
    path: str,
    source: bytes,
    name: str,
) -> Found:
    """The violations inside `name` in this version of `path`, counted per `(code, owner, key)`."""
    policy, _ = load_policy(universe)
    scan = scan_source(universe, path, source, sources_from(manifests, policy, universe))
    span = _source_span(scan, source, name)
    if span is None:
        raise MeizelError(f"{path} defines no top-level function or class {name}")
    found: Counter[tuple[Code, str, str]] = Counter()
    for item in diagnose(scan, manifests, policy=policy):
        if item.debt is not None and span[0] <= item.line <= span[1]:
            found[(item.code, item.debt.owner, item.debt.key)] += 1
    return dict(found)
