# meiva:note
# Typed reads from a mapping a program received untyped.
#
# The gateway client, the lease service, psycopg, GitHub answers, cache files and report rows hand a
# program `dict[str, Any]` or a decoded JSON object. A parameter typed `Mapping[str, object]`
# accepts that without naming the `Any`, and each read here checks the field it hands on, so a
# reader holds an `int` or a `str` instead of an unchecked value.
#
# Pick the reader by what a wrong type means at the call site, in this order:
#
# 1. `int_field` / `str_field` / `bool_field` when the record must carry the field: a missing key or a
#    value of another type is a `TypeError` naming the key.
# 2. `typed_optional_*_field` by default for an optional field. It answers `None` for an absent or
#    null key and raises a `TypeError` naming the key for a value of another type, so a corrupt value
#    is never mistaken for a missing one and the code that follows may safely go on with a default.
# 3. `optional_*_field` only where a wrong type may honestly read as "not given" — a caller that
#    keeps its `.get(...) or default` shape over a record whose producer writes best-effort values.
#    Do not reach for this family to silence a reader: it is the one that hides corruption.
#
# A bool is not a whole number in any family.

from __future__ import annotations

from collections.abc import Mapping

from base.host.boundary.json_values import is_object_dict, is_object_list

__all__ = [
    "bool_field",
    "int_field",
    "optional_bool_field",
    "optional_float_field",
    "optional_int_field",
    "optional_list_field",
    "optional_object_field",
    "optional_str_field",
    "str_field",
    "typed_optional_bool_field",
    "typed_optional_float_field",
    "typed_optional_int_field",
    "typed_optional_list_field",
    "typed_optional_object_field",
    "typed_optional_str_field",
]


# meiva:note
# The whole number `row[key]`; a bool is not one.
def int_field(row: Mapping[str, object], key: str) -> int:
    value = optional_int_field(row, key)
    if value is None:
        raise TypeError(f"{key} is {_described(row, key)}, not int")
    return value


# meiva:note
# The string `row[key]`.
def str_field(row: Mapping[str, object], key: str) -> str:
    value = optional_str_field(row, key)
    if value is None:
        raise TypeError(f"{key} is {_described(row, key)}, not str")
    return value


# meiva:note
# The boolean `row[key]`.
def bool_field(row: Mapping[str, object], key: str) -> bool:
    value = optional_bool_field(row, key)
    if value is None:
        raise TypeError(f"{key} is {_described(row, key)}, not bool")
    return value


def _described(row: Mapping[str, object], key: str) -> str:
    return "missing" if key not in row else type(row[key]).__name__


# meiva:note
# The string `record[key]`, or None when the key is absent or holds another type.
def optional_str_field(record: Mapping[str, object], key: str) -> str | None:
    value = record.get(key)
    return value if isinstance(value, str) else None


# meiva:note
# The whole number `record[key]`, or None when the key is absent or holds another type (a bool is not one).
def optional_int_field(record: Mapping[str, object], key: str) -> int | None:
    value = record.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# meiva:note
# The number `record[key]` as a float, or None when the key is absent or holds another type (a bool is not one).
def optional_float_field(record: Mapping[str, object], key: str) -> float | None:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


# meiva:note
# The boolean `record[key]`, or None when the key is absent or holds another type.
def optional_bool_field(record: Mapping[str, object], key: str) -> bool | None:
    value = record.get(key)
    return value if isinstance(value, bool) else None


# meiva:note
# The JSON object `record[key]`, or None when the key is absent or holds another type.
def optional_object_field(record: Mapping[str, object], key: str) -> dict[str, object] | None:
    value = record.get(key)
    return value if is_object_dict(value) else None


# meiva:note
# The JSON array `record[key]`, or None when the key is absent or holds another type.
def optional_list_field(record: Mapping[str, object], key: str) -> list[object] | None:
    value = record.get(key)
    return value if is_object_list(value) else None


# meiva:note
# The string `record[key]`, or None when the key is absent or null; any other type is a `TypeError`
# naming the key (unlike `optional_str_field`, which reads it as absent).
def typed_optional_str_field(record: Mapping[str, object], key: str) -> str | None:
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{key} is {type(value).__name__}, not str")
    return value


# meiva:note
# The whole number `record[key]`, or None when the key is absent or null; any other type, a bool
# included, is a `TypeError` naming the key.
def typed_optional_int_field(record: Mapping[str, object], key: str) -> int | None:
    value = record.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{key} is {type(value).__name__}, not int")
    return value


# meiva:note
# The number `record[key]` as a float, or None when the key is absent or null; any other type, a bool
# included, is a `TypeError` naming the key.
def typed_optional_float_field(record: Mapping[str, object], key: str) -> float | None:
    value = record.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{key} is {type(value).__name__}, not a number")
    return float(value)


# meiva:note
# The boolean `record[key]`, or None when the key is absent or null; any other type is a `TypeError`
# naming the key (a `0` or a `"false"` is refused, where `optional_bool_field` reads it as absent).
def typed_optional_bool_field(record: Mapping[str, object], key: str) -> bool | None:
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise TypeError(f"{key} is {type(value).__name__}, not bool")
    return value


# meiva:note
# The JSON object `record[key]`, or None when the key is absent or null; any other type is a
# `TypeError` naming the key.
def typed_optional_object_field(record: Mapping[str, object], key: str) -> dict[str, object] | None:
    value = record.get(key)
    if value is None:
        return None
    if not is_object_dict(value):
        raise TypeError(f"{key} is {type(value).__name__}, not an object")
    return value


# meiva:note
# The JSON array `record[key]`, or None when the key is absent or null; any other type is a
# `TypeError` naming the key.
def typed_optional_list_field(record: Mapping[str, object], key: str) -> list[object] | None:
    value = record.get(key)
    if value is None:
        return None
    if not is_object_list(value):
        raise TypeError(f"{key} is {type(value).__name__}, not a list")
    return value
