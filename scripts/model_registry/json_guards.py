# meiva:note
# Narrowing for the tracker's decoded state: a list of strings (the object and list guards live in
# `base.host.boundary.json_values`).

from __future__ import annotations

from typing import TypeGuard

from base.host.boundary.json_values import is_object_list


def is_str_list(value: object) -> TypeGuard[list[str]]:
    return is_object_list(value) and all(isinstance(item, str) for item in value)
