# meiva:note
# Typed reads from a decoded JSON object whose fields may be absent or of another type.
#
# GitHub answers, cache files and report rows arrive as `dict[str, object]` (see
# `scripts.boundary.untyped.is_object_dict`). Each reader here checks the field it hands on and
# answers `None` for an absent key or a value of another type, so a caller keeps its
# `.get(...) or default` shape without holding an unchecked value. A bool is not a whole number.

from __future__ import annotations

from collections.abc import Mapping

from scripts.boundary.untyped import is_object_dict, is_object_list

__all__ = [
    "optional_bool",
    "optional_float",
    "optional_int",
    "optional_list",
    "optional_object",
    "optional_str",
]


# meiva:note
# The string `record[key]`, or None when the key is absent or holds another type.
def optional_str(record: Mapping[str, object], key: str) -> str | None:
    value = record.get(key)
    return value if isinstance(value, str) else None


# meiva:note
# The whole number `record[key]`, or None when the key is absent or holds another type (a bool is not one).
def optional_int(record: Mapping[str, object], key: str) -> int | None:
    value = record.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# meiva:note
# The number `record[key]` as a float, or None when the key is absent or holds another type (a bool is not one).
def optional_float(record: Mapping[str, object], key: str) -> float | None:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


# meiva:note
# The boolean `record[key]`, or None when the key is absent or holds another type.
def optional_bool(record: Mapping[str, object], key: str) -> bool | None:
    value = record.get(key)
    return value if isinstance(value, bool) else None


# meiva:note
# The JSON object `record[key]`, or None when the key is absent or holds another type.
def optional_object(record: Mapping[str, object], key: str) -> dict[str, object] | None:
    value = record.get(key)
    return value if is_object_dict(value) else None


# meiva:note
# The JSON array `record[key]`, or None when the key is absent or holds another type.
def optional_list(record: Mapping[str, object], key: str) -> list[object] | None:
    value = record.get(key)
    return value if is_object_list(value) else None
