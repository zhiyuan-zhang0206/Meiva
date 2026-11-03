# meiva:note
# A call whose answer the standard library types as `Any`, read as `object`.
#
# `json.loads` returns `Any`. A function passed as an argument keeps its signature outside the
# callee, so this accepts it as `Callable[P, object]` and hands back `object`: the caller narrows with
# `isinstance` and the `Any` is never bound to a name. This is a deliberate copy of
# `cli.boundary.untyped.object_of`, which is just as standard-library: the updater package is copied to
# a bare operator checkout, and its import-probe test runs it under `python -S`, where it may import
# no other `cli.*` module and no third-party package. Sharing the function would break that probe;
# do not merge the two.

from __future__ import annotations

from collections.abc import Callable

__all__ = ["object_of"]


# meiva:note
# `function(*args, **kwargs)` as an `object`.
def object_of[**P](function: Callable[P, object], /, *args: P.args, **kwargs: P.kwargs) -> object:
    return function(*args, **kwargs)
