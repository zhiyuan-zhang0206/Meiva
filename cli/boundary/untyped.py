# meiva:note
# A call whose answer the standard library or a stub types as `Any`, read as `object`.
#
# `json.loads`, `yaml.safe_load`, `httpx.Response.json` and `ConfigBoot.get_field` return `Any`. A
# function passed as an argument keeps its signature outside the callee, so this accepts it as
# `Callable[P, object]` and hands back `object`: the caller narrows with `isinstance` instead of
# holding an unchecked value, and the `Any` is never bound to a name. Prefer the typed readers in
# `cli.boundary.json_body` when the answer is JSON; this is for the rest.

from __future__ import annotations

from collections.abc import Callable

__all__ = ["object_of"]


# meiva:note
# `function(*args, **kwargs)` as an `object`.
def object_of[**P](function: Callable[P, object], /, *args: P.args, **kwargs: P.kwargs) -> object:
    return function(*args, **kwargs)
