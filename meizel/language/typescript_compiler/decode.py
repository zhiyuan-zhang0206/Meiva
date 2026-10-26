from __future__ import annotations

import json
from typing import TypeGuard

from meizel.graph.model import (
    Acquisition,
    AmbientKind,
    AnyCollaboratorSite,
    Code,
    EnumSubsetUse,
    Fact,
    FactKind,
    Patch,
    Problem,
    PurityItem,
    Restricted,
    TypeCastSite,
)
from meizel.language.typescript_compiler.model import TsScan


def _is_object(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _is_array(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _record(value: object, fields: str) -> dict[str, object]:
    if not _is_object(value) or set(value) != set(fields.split()):
        raise ValueError(f"expected exactly these compiler fields: {fields}")
    return value


def _array(value: object) -> list[object]:
    if not _is_array(value):
        raise ValueError("compiler sequence must be an array")
    return value


def _row(value: object, size: int) -> list[object]:
    row = _array(value)
    if len(row) != size:
        raise ValueError(f"compiler record must have {size} fields")
    return row


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("compiler field must be a string")
    return value


def _number(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("compiler position must be a nonnegative integer")
    return value


def _strings(value: object) -> tuple[str, ...]:
    return tuple(_text(item) for item in _array(value))


def _source(value: object) -> TsScan:
    r = _record(
        value,
        "path facts external patches purity problems exports defined ambient casts any subsets symbols restricted",
    )
    path = _text(r["path"])
    facts: list[Fact] = []
    for item in _array(r["facts"]):
        dst, line, kind, names, detail = _row(item, 5)
        facts.append(
            Fact(
                path,
                _text(dst),
                _number(line),
                FactKind(_text(kind)),
                _strings(names),
                _text(detail),
            )
        )
    patches: list[Patch] = []
    for item in _array(r["patches"]):
        line, form, target, code = _row(item, 4)
        patches.append(Patch(_number(line), _text(form), _text(target), Code(_text(code))))
    purity: list[PurityItem] = []
    for item in _array(r["purity"]):
        line, code, key, message, hint = _row(item, 5)
        purity.append(
            PurityItem(_number(line), Code(_text(code)), _text(key), _text(message), _text(hint))
        )
    problems: list[Problem] = []
    for item in _array(r["problems"]):
        line, code, message = _row(item, 3)
        problems.append(Problem(_number(line), Code(_text(code)), _text(message)))
    ambient: list[Acquisition] = []
    for item in _array(r["ambient"]):
        line, kind, target = _row(item, 3)
        ambient.append(Acquisition(_number(line), AmbientKind(_text(kind)), _text(target)))
    casts: list[TypeCastSite] = []
    for item in _array(r["casts"]):
        line, column, name = _row(item, 3)
        casts.append(TypeCastSite(_number(line), _number(column), _text(name)))
    collaborators: list[AnyCollaboratorSite] = []
    for item in _array(r["any"]):
        line, column, name, slot, role = _row(item, 5)
        collaborators.append(
            AnyCollaboratorSite(
                _number(line), _number(column), _text(name), _text(slot), _text(role)
            )
        )
    subsets: list[EnumSubsetUse] = []
    for item in _array(r["subsets"]):
        line, name, members = _row(item, 3)
        subsets.append(EnumSubsetUse(_number(line), _text(name), _strings(members)))
    symbols: list[tuple[str, int, int]] = []
    for item in _array(r["symbols"]):
        name, start, end = _row(item, 3)
        symbols.append((_text(name), _number(start), _number(end)))
    restricted: list[Restricted] = []
    for item in _array(r["restricted"]):
        line, form, detail = _row(item, 3)
        restricted.append(Restricted(_number(line), _text(form), _text(detail)))
    exports = None if r["exports"] is None else frozenset(_strings(r["exports"]))
    return TsScan(
        path,
        tuple(facts),
        frozenset(_strings(r["external"])),
        tuple(patches),
        tuple(purity),
        tuple(problems),
        exports,
        frozenset(_strings(r["defined"])),
        tuple(ambient),
        tuple(casts),
        tuple(collaborators),
        tuple(subsets),
        tuple(symbols),
        tuple(restricted),
    )


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate compiler field: {key}")
        result[key] = value
    return result


def decode_batch(text: str, expected: frozenset[str]) -> dict[str, TsScan]:
    raw: object = json.loads(text, object_pairs_hook=_unique)
    result = _record(raw, "version sources")
    if type(result["version"]) is not int or result["version"] != 1:
        raise ValueError("unsupported TypeScript compiler result version")
    scans = [_source(item) for item in _array(result["sources"])]
    paths = [scan.path for scan in scans]
    if len(set(paths)) != len(paths) or set(paths) != set(expected):
        raise ValueError("TypeScript compiler returned a different source set")
    return {scan.path: scan for scan in scans}
