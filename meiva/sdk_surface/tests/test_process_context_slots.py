# meiva:note
# The SDK's process-context slot has three states: never written, written with a context (or a
# process purpose), and written with something else. Only the first two are normal; these tests
# pin that the third raises instead of reading as "no context", so a tampered slot cannot make a
# caller believe this process is unbound.

from __future__ import annotations

from collections.abc import Callable

import pytest

from meiva.sdk_surface import CONTEXT_KEY, meiva_module, process_context

_ABSENT = object()


# meiva:note
# `meiva_module().__dict__` is `dict[str, Any]`; `dict(...)` narrows the view so the reads below
# answer `object` instead of `Any`.
def _slots() -> dict[str, object]:
    return dict(meiva_module().__dict__)


# meiva:note
# Put `value` in the context slot and return the previous entry (`_ABSENT` when none).
def _write_slot(value: object) -> object:
    previous = _slots().get(CONTEXT_KEY, _ABSENT)
    meiva_module().__dict__[CONTEXT_KEY] = value
    return previous


def _restore_slot(previous: object) -> None:
    slots = meiva_module().__dict__
    if previous is _ABSENT:
        slots.pop(CONTEXT_KEY, None)
    else:
        slots[CONTEXT_KEY] = previous


@pytest.mark.parametrize(
    "read",
    [
        process_context.bound_context,
        process_context.require_bound_context,
        process_context.unbind_context,
    ],
    ids=["bound_context", "require_bound_context", "unbind_context"],
)
def test_a_wrong_typed_context_slot_raises_instead_of_reading_as_unbound(
    read: Callable[[], object],
) -> None:
    previous = _write_slot("not a context")
    try:
        with pytest.raises(TypeError, match="MeivaContext"):
            read()
    finally:
        _restore_slot(previous)


def test_an_unwritten_context_slot_is_still_unbound() -> None:
    previous = _slots().get(CONTEXT_KEY, _ABSENT)
    meiva_module().__dict__.pop(CONTEXT_KEY, None)
    try:
        assert process_context.bound_context() is None
        assert process_context.unbind_context() is None
    finally:
        _restore_slot(previous)
