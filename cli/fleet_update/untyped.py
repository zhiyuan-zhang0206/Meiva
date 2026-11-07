# meiva:note
# A call whose answer the standard library types as `Any`, read as `object`.
#
# `json.loads` returns `Any`. A function passed as an argument keeps its signature outside the
# callee, so this accepts it as `Callable[P, object]` and hands back `object`: the caller narrows with
# `isinstance` and the `Any` is never bound to a name.

from __future__ import annotations

from collections.abc import Callable

__all__ = ["object_of"]


# meiva:note
# `function(*args, **kwargs)` as an `object`.
def object_of[**P](function: Callable[P, object], /, *args: P.args, **kwargs: P.kwargs) -> object:
    return function(*args, **kwargs)
