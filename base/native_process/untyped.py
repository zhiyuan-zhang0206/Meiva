# meiva:note
# A call the standard library or a stub types as `Any`, read as `object`, and a decoded JSON object.
# `base/host/boundary/untyped.py` owns the same idea for the layers above; `base/host` imports this
# package, so the package cannot import that one and keeps these two entries of its own.
from __future__ import annotations

from collections.abc import Callable
from typing import TypeGuard

__all__ = ["is_object_dict", "object_of"]


# meiva:note
# `function(*args, **kwargs)` as an `object`: ctypes foreign functions answer `Any`, and a parameter
# annotated `Callable[P, object]` takes the function without naming it.
def object_of[**P](function: Callable[P, object], /, *args: P.args, **kwargs: P.kwargs) -> object:
    return function(*args, **kwargs)


# meiva:note
# A decoded JSON object: a dict whose keys are all strings, values still `object`.
def is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return _is_dict(value) and all(isinstance(key, str) for key in value)


def _is_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)
