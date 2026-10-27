# meiva:note
# What a plugin's SDK wrap and its dotted paths are, decided over the callables and strings the
# installer read: whether a path names a public member, how the chained function presents
# itself, whether a layer is async and when a firing is reported. The registration primitive
# (`wraps.py`) resolves the target in the live `meiva` module and writes the replacement; these
# functions only choose, so the presentation rules and the counting rule are tested as values.
# meiva:link meiva/docs/sdk-surface/sdk-surface.meiva.okf.md

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass


# meiva:note
# Whether `path` is a dotted path of plain public identifiers (`files.read`, `shell.sessions`):
# non-empty, each segment an identifier without an underscore prefix. The one rule behind a
# wrap target and an SDK expansion path.
def is_public_dotted_path(path: str) -> bool:
    return bool(path) and all(
        segment.isidentifier() and not segment.startswith("_") for segment in path.split(".")
    )


# meiva:note
# The refusal for a wrap target that resolved to something that cannot be called; None when it
# can. Wrapping a constant or a namespace is a plugin bug, caught at registration.
def uncallable_message(target: str, current: object) -> str | None:
    if callable(current):
        return None
    return f"meiva.{target} is {type(current).__name__}, not callable — wrap targets are functions."


# meiva:note
# Whether a layer that called `inner` `inner_calls` times is reported. A layer that calls it
# exactly once only passes through, and it always runs once installed, so counting it would
# measure the installation rather than the shim.
def activation_due(inner_calls: int) -> bool:
    return inner_calls != 1


# meiva:note
# Whether the chained function must be a coroutine function: the callable it replaces or the
# wrapper is one, so a caller awaiting the target keeps awaiting it.
def layer_is_async(current: Callable[..., object], wrapper: Callable[..., object]) -> bool:
    return inspect.iscoroutinefunction(current) or inspect.iscoroutinefunction(wrapper)


# meiva:note
# How the chained function presents itself, so the SDK stub renders `def read(...)` and the
# repr stays `meiva.files`. `name`, `qualname` and `module` come from the callable replaced,
# falling back to the wrapper; `doc` is the wrapper's own when it wrote one, else the replaced
# callable's; `inherited` are the replaced callable's function-attached members, which the
# chained function keeps unless it has its own of that name; `signature` is the wrapper's
# without its leading `inner` parameter, None when the wrapper has no signature to show.
@dataclass(frozen=True, slots=True)
class ChainedFace:
    name: str
    qualname: str
    module: str
    doc: str | None
    inherited: Mapping[str, object]
    signature: inspect.Signature | None


def chained_face(wrapper: Callable[..., object], current: Callable[..., object]) -> ChainedFace:
    name: str = getattr(current, "__name__", getattr(wrapper, "__name__", "wrapped"))
    signature: inspect.Signature | None
    try:
        declared = inspect.signature(wrapper)
    except (ValueError, TypeError):
        signature = None
    else:
        params = list(declared.parameters.values())
        signature = declared.replace(parameters=params[1:]) if params else None
    return ChainedFace(
        name=name,
        qualname=getattr(current, "__qualname__", name),
        module=getattr(current, "__module__", getattr(wrapper, "__module__", "")),
        doc=wrapper.__doc__ or getattr(current, "__doc__", None),
        inherited=dict(getattr(current, "__dict__", {})),
        signature=signature,
    )
