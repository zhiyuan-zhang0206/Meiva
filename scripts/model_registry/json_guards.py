# meiva:note
# Narrowing for decoded JSON: provider responses and the tracker state file are `object` until checked.

from __future__ import annotations

from typing import Any, TypeGuard


def is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def is_object_dict(value: object) -> TypeGuard[dict[str, Any]]:
    return is_raw_dict(value) and all(isinstance(key, str) for key in value)


def is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def is_str_list(value: object) -> TypeGuard[list[str]]:
    return is_object_list(value) and all(isinstance(item, str) for item in value)
