# meiva:note
# Values the standard library or a stub types as `Any`, read as `object` for the caller to narrow.
#
# `json.loads`, `yaml.safe_load` and `ast.literal_eval` return `Any`. `object_of` accepts the
# function as an argument, so the `Any` never becomes the value of an expression the caller binds;
# the `is_object_*` guards then check the container and its keys. Until `base` owns these, this is
# the one copy `scripts/` reads from.

from __future__ import annotations

from collections.abc import Callable
from typing import TypeGuard

__all__ = ["is_object_dict", "is_object_list", "object_of"]


# meiva:note
# `function(*args, **kwargs)` as an `object`.
def object_of[**P](function: Callable[P, object], /, *args: P.args, **kwargs: P.kwargs) -> object:
    return function(*args, **kwargs)


def _is_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


# meiva:note
# A decoded JSON (or YAML) object: a dict whose keys are all strings, with the values still
# `object` until a reader narrows them.
def is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return _is_dict(value) and all(isinstance(key, str) for key in value)


# meiva:note
# A decoded JSON (or YAML) array whose elements are still `object` until a reader narrows them.
def is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)
