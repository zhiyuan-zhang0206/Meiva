from __future__ import annotations

import json
import typing
from collections.abc import Callable


def parse_json(text: str) -> object:
    return _read(json.loads, text)


def _read(loader: Callable[[str], object], text: str) -> object:
    return loader(text)


def _is_dict(value: object) -> typing.TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_list(value: object) -> typing.TypeGuard[list[object]]:
    return isinstance(value, list)


def as_object(value: object) -> dict[str, object]:
    if not _is_dict(value):
        raise TypeError(f"expected a JSON object, got {type(value).__name__}")
    out: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError(f"expected string keys, got {key!r}")
        out[key] = item
    return out


def as_list(value: object) -> list[object]:
    if not _is_list(value):
        raise TypeError(f"expected a JSON array, got {type(value).__name__}")
    return list(value)


def as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError(f"expected an integer, got {value!r}")
    return int(value)


def as_str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected a string, got {value!r}")
    return value
