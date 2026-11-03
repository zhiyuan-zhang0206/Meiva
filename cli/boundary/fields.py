# meiva:note
# Typed reads from a mapping the command received untyped.
#
# The gateway client, the lease service and psycopg hand a command `dict[str, Any]`. A parameter
# typed `Mapping[str, object]` accepts that without naming the `Any`, and each read here checks
# the field it hands on, so a reader holds an `int` or a `str` instead of an unchecked value. A
# missing key or a value of another type is a `TypeError` naming the key.

from __future__ import annotations

from collections.abc import Mapping

__all__ = ["bool_field", "int_field", "str_field"]


# meiva:note
# The whole number `row[key]`; a bool is not one.
def int_field(row: Mapping[str, object], key: str) -> int:
    value = row[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{key} is {type(value).__name__}, not int")
    return value


# meiva:note
# The string `row[key]`.
def str_field(row: Mapping[str, object], key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} is {type(value).__name__}, not str")
    return value


# meiva:note
# The boolean `row[key]`.
def bool_field(row: Mapping[str, object], key: str) -> bool:
    value = row[key]
    if not isinstance(value, bool):
        raise TypeError(f"{key} is {type(value).__name__}, not bool")
    return value
