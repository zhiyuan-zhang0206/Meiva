# meiva:note
# Hints about the shape of a value class: a class that bundles several callables
# (`callable-bag`) and a frozen dataclass that holds a mutable member
# (`mutable-in-frozen`). A class fires when the change added its definition line or
# one of the offending field lines.
from __future__ import annotations

import ast

from scripts.lint.diagnostics.diff_hints.model import FileKind, HintId, Site, Unit
from scripts.lint.diagnostics.diff_hints.names import (
    annotation_names,
    decorator_name,
    type_head,
    union_members,
)

_BAG_BASES = frozenset({"NamedTuple", "TypedDict", "Protocol", "BaseModel"})
_MUTABLE_HEADS = frozenset(
    {
        "list",
        "dict",
        "set",
        "List",
        "Dict",
        "Set",
        "MutableMapping",
        "MutableSequence",
        "MutableSet",
        "bytearray",
        "deque",
        "defaultdict",
        "OrderedDict",
        "Counter",
        "object",
        "Any",
    }
)


def _fields(node: ast.ClassDef) -> list[ast.AnnAssign]:
    return [
        stmt
        for stmt in node.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    ]


def _is_dataclass(node: ast.ClassDef, unit: Unit) -> bool:
    return any(
        decorator_name(decorator, unit.names) == "dataclasses.dataclass"
        for decorator in node.decorator_list
    )


def _is_frozen_dataclass(node: ast.ClassDef, unit: Unit) -> bool:
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        if decorator_name(decorator, unit.names) != "dataclasses.dataclass":
            continue
        for keyword in decorator.keywords:
            frozen = isinstance(keyword.value, ast.Constant) and keyword.value.value is True
            if keyword.arg == "frozen" and frozen:
                return True
    return False


def _is_value_class(node: ast.ClassDef, unit: Unit) -> bool:
    bases = {type_head(base) for base in node.bases}
    return _is_dataclass(node, unit) or bool(bases & _BAG_BASES)


def callable_bag(unit: Unit) -> list[Site]:
    sites: list[Site] = []
    for node in unit.classes:
        if not _is_value_class(node, unit):
            continue
        callables = [f for f in _fields(node) if "Callable" in annotation_names(f.annotation)]
        touched = node.lineno in unit.added or any(f.lineno in unit.added for f in callables)
        if len(callables) >= 2 and touched:
            names = ", ".join(
                f"`{f.target.id}`" for f in callables if isinstance(f.target, ast.Name)
            )
            detail = f"`{node.name}` bundles {len(callables)} callables: {names}"
            sites.append(Site(HintId.CALLABLE_BAG, unit.path, node.lineno, detail))
    return sites


def _mutable_head(annotation: ast.expr) -> str | None:
    if "ClassVar" in annotation_names(annotation):
        return None
    for member in union_members(annotation):
        head = type_head(member)
        if head in _MUTABLE_HEADS:
            return head
    return None


def mutable_in_frozen(unit: Unit) -> list[Site]:
    if unit.kind is not FileKind.PRODUCTION:
        return []
    sites: list[Site] = []
    for node in unit.classes:
        if not _is_frozen_dataclass(node, unit):
            continue
        for field in _fields(node):
            head = _mutable_head(field.annotation)
            if (
                head is not None
                and field.lineno in unit.added
                and isinstance(field.target, ast.Name)
            ):
                detail = f"frozen `{node.name}.{field.target.id}` holds a mutable `{head}`"
                sites.append(Site(HintId.MUTABLE_IN_FROZEN, unit.path, field.lineno, detail))
    return sites
