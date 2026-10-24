# meiva:link scripts/structure/imports/README.md

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from . import bindings

__all__ = ["Discovery", "Membership", "prove"]


@dataclass(frozen=True)
class Membership:
    # meiva:link scripts/structure/imports/README.md

    anchor: ast.expr
    target: str
    member: str


@dataclass(frozen=True)
class Discovery:
    members: tuple[Membership, ...]
    unanchored: tuple[ast.expr, ...]


def _loads(tree: ast.AST, name: str) -> set[int]:
    return {
        id(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)
    }


def _hidden(node: ast.AST, entry: str) -> bool:
    match node:
        case ast.If(
            test=ast.Call(
                func=ast.Attribute(
                    value=ast.Attribute(value=ast.Name(id=name), attr="name"), attr="startswith"
                ),
                args=[ast.Constant(value=".")],
                keywords=[],
            ),
            body=[ast.Continue()],
            orelse=[],
        ):
            return name == entry
        case _:
            return False


def _collected_member(
    body: list[ast.stmt], entry: str, scope: bindings.Scope, tree: ast.AST
) -> str | None:
    match body:
        case [
            ast.Assign(
                targets=[ast.Name(id=member)],
                value=ast.BinOp(
                    left=ast.Name(id=source), op=ast.Div(), right=ast.Constant(value=str(filename))
                ),
            ),
            ast.If(
                test=ast.Call(
                    func=ast.Attribute(value=ast.Name(id=read) as checked, attr="is_file"),
                    args=[],
                    keywords=[],
                ),
                body=[
                    ast.Expr(
                        value=ast.Call(
                            func=ast.Attribute(value=ast.Name() as output, attr="append"),
                            args=[ast.Name(id=emitted) as appended],
                            keywords=[],
                        )
                    )
                ],
                orelse=[],
            ),
        ] if source == entry and read == member and emitted == member:
            pass
        case _:
            return None
    if filename in {"", ".", ".."} or "/" in filename or "\\" in filename:
        return None
    if scope.stores[member] != 1 or scope.stores[output.id] != 1:
        return None
    match scope.value(output):
        case ast.List(elts=[]):
            pass
        case _:
            return None
    match tree:
        case (
            ast.FunctionDef(body=[*_, ast.Return(value=ast.Name(id=returned) as result)])
            | ast.AsyncFunctionDef(body=[*_, ast.Return(value=ast.Name(id=returned) as result)])
        ) if returned == output.id:
            allowed_output = {id(output), id(result)}
        case _:
            return None
    if _loads(tree, output.id) != allowed_output:
        return None
    if _loads(tree, member) != {id(checked), id(appended)}:
        return None
    return filename


def _member_loop(
    node: ast.For, scope: bindings.Scope, tree: ast.AST, *, expected_root: str
) -> tuple[ast.Name, str] | None:
    match node:
        case ast.For(
            target=ast.Name(id=entry),
            iter=ast.Call(
                func=ast.Name(id="sorted"),
                args=[
                    ast.Call(
                        func=ast.Attribute(value=ast.Name() as root, attr="iterdir"),
                        args=[],
                        keywords=[],
                    )
                ],
                keywords=[],
            ),
            body=body,
            orelse=[],
        ) if root.id == expected_root and scope.stores[entry] == 1 and not scope.bound("sorted"):
            pass
        case _:
            return None
    hidden = bool(body) and _hidden(body[0], entry)
    rest = body[1:] if hidden else body
    filename = _collected_member(rest, entry, scope, tree)
    if filename is None:
        return None
    allowed = _loads(rest[0], entry) | (_loads(body[0], entry) if hidden else set[int]())
    return (root, filename) if _loads(tree, entry) == allowed else None


def _root_loop(node: ast.AST) -> tuple[str, ast.Name, ast.For, list[ast.expr]] | None:
    match node:
        case ast.For(
            target=ast.Name(id=root),
            iter=ast.Tuple(elts=choices) | ast.List(elts=choices),
            body=[
                ast.If(
                    test=ast.UnaryOp(
                        op=ast.Not(),
                        operand=ast.Call(
                            func=ast.Attribute(value=ast.Name(id=read) as checked, attr="is_dir"),
                            args=[],
                            keywords=[],
                        ),
                    ),
                    body=[ast.Continue()],
                    orelse=[],
                ),
                ast.For() as inner,
            ],
            orelse=[],
        ) if root == read:
            return root, checked, inner, choices
        case _:
            return None


def _anchors(
    choices: list[ast.expr],
    tree: ast.AST,
    scope: bindings.Scope,
    resolve: Callable[[ast.expr], str | None],
) -> tuple[list[tuple[ast.expr, str]], tuple[ast.expr, ...]]:
    anchored: list[tuple[ast.expr, str]] = []
    unanchored: list[ast.expr] = []
    for alternative in choices:
        target = resolve(alternative)
        if target is None:
            unanchored.append(alternative)
            continue
        if isinstance(alternative, ast.Name) and (
            alternative.id not in scope.values
            or scope.stores[alternative.id] != 1
            or _loads(tree, alternative.id) != {id(alternative)}
        ):
            continue
        anchor = scope.value(alternative)
        if any(
            isinstance(node, ast.Name)
            and node.id != "__file__"
            and scope.origin(node) not in {"pathlib.Path", "pathlib"}
            for node in ast.walk(anchor)
        ):
            continue
        path = PurePosixPath(target)
        if target and not path.is_absolute() and ".." not in path.parts:
            anchored.append((anchor, target))
    return anchored, tuple(unanchored)


def _replacement(node: ast.AST) -> bool:
    match node:
        case ast.Attribute(ctx=ast.Store() | ast.Del()) | ast.Global() | ast.Nonlocal():
            return True
        case ast.Name(id=name) | ast.Attribute(attr=name) | ast.alias(name=name):
            return name.rsplit(".", maxsplit=1)[-1] in {
                "*",
                "__setattr__",
                "__delattr__",
                "setattr",
                "delattr",
                "getattr",
                "vars",
                "exec",
                "eval",
                "globals",
                "locals",
                "__dict__",
                "__globals__",
                "__builtins__",
                "__getattribute__",
                "patch",
            }
        case _:
            return False


def _unmodified(scope: bindings.Scope, module_tree: ast.AST) -> bool:
    if any(_replacement(node) for node in ast.walk(module_tree)):
        return False
    current: bindings.Scope | None = scope
    while current is not None:
        if current.context is not None and any(
            name in current.context.mutated
            for name, origin in current.origins.items()
            if origin in {"pathlib.Path", "pathlib"}
        ):
            return False
        current = current.parent
    return True


def prove(
    tree: ast.AST,
    scope: bindings.Scope,
    resolve: Callable[[ast.expr], str | None],
    *,
    module_tree: ast.AST,
) -> Discovery:
    # meiva:link scripts/structure/imports/README.md
    if not isinstance(tree, ast.FunctionDef | ast.AsyncFunctionDef):
        return Discovery((), ())
    inputs = [
        (outer, *_anchors(outer[3], tree, scope, resolve))
        for statement in tree.body
        if (outer := _root_loop(statement)) is not None
    ]
    unanchored = tuple(choice for _, _, opaque in inputs for choice in opaque)
    if not inputs or tree.decorator_list or not _unmodified(scope, module_tree):
        return Discovery((), unanchored)
    found: list[Membership] = []
    for (root, checked, inner, _), anchored, _ in inputs:
        shape = _member_loop(inner, scope, tree, expected_root=root)
        if shape is None or scope.stores[root] != 1:
            continue
        if _loads(tree, root) != {id(checked), id(shape[0])}:
            continue
        found.extend(Membership(anchor, target, shape[1]) for anchor, target in anchored)
    return Discovery(tuple(found), unanchored)
