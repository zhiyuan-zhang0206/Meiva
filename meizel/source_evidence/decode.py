# meiva:note
# This boundary accepts only the version-one supplied-index JSON shape and freezes every sequence.
# No application schema dependency is introduced into the standard-library-only Meizel process.
# meiva:link meizel/docs/checker/typescript-evidence.meiva.okf.md

from __future__ import annotations

import json
from typing import TypeGuard

from meizel.source_evidence.model import Declaration, Identity, Index, Observation, Source


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_table(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(k, str) for k in value)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _object(value: object, fields: str) -> dict[str, object]:
    if not _is_table(value):
        raise TypeError(f"expected TS index object: {fields}")
    if set(value) != set(fields.split()):
        raise ValueError(f"expected exactly these TS index fields: {fields}")
    return value


def _sequence(value: object) -> list[object]:
    if not _is_object_list(value):
        raise TypeError("TS index sequence must be an array")
    return value


def _string(value: object, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise ValueError("TS index string must be nonempty")
    return value


def _line(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("TS index line must be a positive integer")
    return value


def _sha(value: object) -> str:
    text = _string(value)
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise ValueError("TS index digest must be lowercase SHA-256")
    return text


def _declarations(value: object) -> tuple[Declaration, ...]:
    declarations: list[Declaration] = []
    for item in _sequence(value):
        record = _object(item, "dependency line")
        declarations.append(Declaration(_string(record["dependency"]), _line(record["line"])))
    return tuple(declarations)


def _observation(value: object) -> Observation:
    record = _object(
        value,
        "line end_line binding properties kind constructor declarations "
        "frozen_surface freeze_receiver freeze_callee reason",
    )
    kind = record["kind"]
    constructor = record["constructor"]
    frozen = record["frozen_surface"]
    if kind not in ("module-allocation", "unknown") or constructor not in ("Map", "Set", "unknown"):
        raise ValueError("unknown TS observation kind or constructor")
    if type(frozen) is not bool:
        raise ValueError("TS frozen surface must be a boolean")
    return Observation(
        _line(record["line"]),
        _line(record["end_line"]),
        _string(record["binding"], empty=True),
        tuple(_string(p, empty=True) for p in _sequence(record["properties"])),
        kind,
        constructor,
        _declarations(record["declarations"]),
        frozen,
        _declarations(record["freeze_receiver"]),
        _declarations(record["freeze_callee"]),
        _string(record["reason"]),
    )


def _source(value: object) -> Source:
    record = _object(value, "path sha256 status reason observations")
    status = record["status"]
    if status not in ("observed", "unavailable"):
        raise ValueError("unknown TS source status")
    return Source(
        _string(record["path"]),
        _sha(record["sha256"]),
        status,
        _string(record["reason"]),
        tuple(_observation(o) for o in _sequence(record["observations"])),
    )


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"duplicate TS index field: {name}")
        result[name] = value
    return result


def decode_index(text: str) -> Index:
    raw: object = json.loads(text, object_pairs_hook=_unique)
    record = _object(raw, "version kind root compiler_version dependencies sources")
    if type(record["version"]) is not int or record["version"] != 1:
        raise ValueError("unsupported TS index version")
    if record["kind"] != "meizel-ts-holders":
        raise ValueError("unsupported TS index kind")
    dependencies: list[Identity] = []
    for item in _sequence(record["dependencies"]):
        dependency = _object(item, "path sha256")
        dependencies.append(Identity(_string(dependency["path"]), _sha(dependency["sha256"])))
    return Index(
        1,
        "meizel-ts-holders",
        _string(record["root"]),
        _string(record["compiler_version"]),
        tuple(dependencies),
        tuple(_source(source) for source in _sequence(record["sources"])),
    )
