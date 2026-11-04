# meiva:note
# What the SDK usage recorder is and where it goes, decided over the callables the installer
# read: whether a callable is a recorder, whether it belongs to another sampling owner, what
# stripping the recorders leaves and what an uninstall puts back. The metering shell (`metering.py`)
# walks the live namespace and writes the recorders; these functions only choose, so the
# identity rule that tells a recorder from a wrapper that copied its attributes is tested as
# values.
# meiva:link meiva/docs/sdk-surface/sdk-surface.meiva.okf.md

from __future__ import annotations

from collections.abc import Callable

from base.host.boundary.untyped import object_of

# meiva:note
# A recorder marks itself with a reference to itself, so it is recognised by identity: a plugin
# wrapper built over a recorder inherits the marker through `__dict__`, but there it points at
# the inner recorder, not at the wrapper.
RECORDER_MARK = "__meiva_recorder__"
SAMPLING_MARK = "__meiva_sampling_owner__"


def is_recorder(fn: object) -> bool:
    return fn is not None and getattr(fn, RECORDER_MARK, None) is fn


# meiva:note
# Whether `fn` is a recorder that belongs to a sampling owner other than `sampling`: installing
# over it would meter one call under two owners.
def foreign_recorder(fn: object, sampling: object) -> bool:
    return is_recorder(fn) and getattr(fn, SAMPLING_MARK) is not sampling


# meiva:note
# `current` with every recorder layer stripped. A recorder always carries `__wrapped__`
# (`functools.wraps`); a plugin wrap chained over one would keep a stale recorder alive.
# meiva:note
# The callable a recorder wraps. `None` means "this object carries no `__wrapped__` at all" — a
# wrapper that is not a recorder. A `__wrapped__` that exists but is not callable is a broken
# recorder, and reading it as "nothing to unwrap" would silently leave a recorder installed.
_MISSING = object()


def _wrapped(fn: object) -> Callable[..., object] | None:
    inner = object_of(getattr, fn, "__wrapped__", _MISSING)
    if inner is _MISSING:
        return None
    if not callable(inner):
        raise TypeError(f"a recorder's __wrapped__ must be callable, got {type(inner).__name__}")
    return inner


def strip_recorders(current: Callable[..., object]) -> Callable[..., object]:
    while is_recorder(current):
        inner = _wrapped(current)
        if inner is None:
            raise TypeError(f"the recorder {current!r} carries no __wrapped__ to strip")
        current = inner
    return current


# meiva:note
# What an uninstall puts back at a target: the callable the recorder wraps, or None when the
# target holds no recorder any more (replaced or removed since), which is then left alone.
def restore_target(current: object) -> object | None:
    if not is_recorder(current):
        return None
    inner = _wrapped(current)
    if inner is None:
        raise TypeError(f"the recorder {current!r} carries no __wrapped__ to restore")
    return inner
