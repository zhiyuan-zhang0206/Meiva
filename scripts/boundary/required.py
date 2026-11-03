# meiva:note
# Required typed reads from a decoded JSON object: an absent key or a value of another type is a
# `TypeError` naming the key, where `scripts.boundary.fields` answers `None`.

from __future__ import annotations

from collections.abc import Mapping

from scripts.boundary.fields import optional_int, optional_str

__all__ = ["required_int", "required_str"]


# meiva:note
# The whole number `record[key]`; an absent key or a value of another type (a bool is not one) is a `TypeError` naming the key.
def required_int(record: Mapping[str, object], key: str) -> int:
    value = optional_int(record, key)
    if value is None:
        raise TypeError(f"{key} is {type(record.get(key)).__name__}, not int")
    return value


# meiva:note
# The string `record[key]`; an absent key or a value of another type is a `TypeError` naming the key.
def required_str(record: Mapping[str, object], key: str) -> str:
    value = optional_str(record, key)
    if value is None:
        raise TypeError(f"{key} is {type(record.get(key)).__name__}, not str")
    return value
