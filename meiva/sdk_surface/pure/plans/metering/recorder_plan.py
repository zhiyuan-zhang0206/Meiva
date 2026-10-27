# meiva:note
# What the SDK usage recorder is and where it goes, decided over the callables the installer
# read: whether a callable is a recorder, whether it belongs to another sampling owner, which
# targets still need one and what an uninstall puts back. The metering shell (`metering.py`)
# walks the live namespace and writes the recorders; these functions only choose, so the
# identity rule that tells a recorder from a wrapper that copied its attributes is tested as
# values.
# meiva:link meiva/docs/sdk-surface/sdk-surface.meiva.okf.md

from __future__ import annotations

from collections.abc import Callable

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
def strip_recorders(current: Callable[..., object]) -> Callable[..., object]:
    while is_recorder(current):
        current = vars(current)["__wrapped__"]
    return current


# meiva:note
# Whether a routine found at a target still needs a recorder: it exists and is not one already.
def needs_recorder(current: object) -> bool:
    return current is not None and not is_recorder(current)


# meiva:note
# What an uninstall puts back at a target: the callable the recorder wraps, or None when the
# target holds no recorder any more (replaced or removed since), which is then left alone.
def restore_target(current: object) -> object | None:
    if current is not None and is_recorder(current):
        return vars(current)["__wrapped__"]
    return None
