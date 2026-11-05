# meiva:note
# A call whose answer the standard library or a stub types as `Any`, read as `object`.
#
# `json.loads`, `yaml.safe_load`, `httpx.Response.json` and `ConfigBoot.get_field` return `Any`. A
# function passed as an argument keeps its signature outside the callee, so this accepts it as
# `Callable[P, object]` and hands back `object`: the caller narrows with `isinstance` (or
# `base.host.boundary.json_values`) instead of holding an unchecked value, and the `Any` is never bound
# to a name. Prefer a `TypeAdapter` or a pydantic model when the shape is known; this is for the
# rest.
#
# DELIBERATE TWIN, not an accident and not an oversight. The same shape is copied in
# `meizel/source_text.py` -- the checker runs from pre-commit and from `check.sh` inside a bare
# checkout, where no `base` package exists -- and in `cli/fleet_update/untyped.py`, whose package is
# copied to a bare operator checkout and probed under `python -S`, where it may import no other
# first-party module. Neither copy may import this one, so the three move together: a change to the
# rules of one belongs in all three, and merging them breaks a bare checkout.

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

__all__ = ["object_mapping", "object_of", "object_sequence"]


# meiva:note
# `function(*args, **kwargs)` as an `object`.
def object_of[**P](function: Callable[P, object], /, *args: P.args, **kwargs: P.kwargs) -> object:
    return function(*args, **kwargs)


# meiva:note
# A mapping a library types with `Any` values (`RunnableConfig["configurable"]`, a checkpoint's
# `channel_values`), read with `object` values: the reader narrows each entry it uses instead of
# holding an unchecked one. The parameter annotation does the work; assigning the mapping to a local
# annotated `Mapping[str, object]` would narrow back to the declared `Any`-valued type.
def object_mapping(mapping: Mapping[str, object], /) -> Mapping[str, object]:
    return mapping


# meiva:note
# The sequence twin of `object_mapping`: a list a library types `list[Any]`, read as `Sequence[object]`.
def object_sequence(items: Sequence[object], /) -> Sequence[object]:
    return items
