# meiva:note
# `meiva/sdk_surface/pure/plans/metering/recorder_plan.py` unit tests: what makes a callable a
# metering recorder (by identity, not by a copied attribute), which targets still need one and what
# an uninstall puts back. Asserted by value; the installed recorders are covered by
# `meiva/tests/namespaces/sdk_surface/test_metering.py`.

from __future__ import annotations

import functools
from collections.abc import Callable

from meiva.sdk_surface.pure.plans.metering.recorder_plan import (
    RECORDER_MARK,
    SAMPLING_MARK,
    foreign_recorder,
    is_recorder,
    restore_target,
    strip_recorders,
)


def _base(value: int) -> int:
    return value


def _recorder_over(inner: Callable[..., int], owner: object) -> Callable[..., int]:
    @functools.wraps(inner)
    def recorder(*args: int) -> int:
        return inner(*args)

    setattr(recorder, RECORDER_MARK, recorder)
    setattr(recorder, SAMPLING_MARK, owner)
    return recorder


def _plugin_wrapper_over(inner: Callable[..., int]) -> Callable[..., int]:
    @functools.wraps(inner)
    def wrapper(*args: int) -> int:
        return inner(*args)

    return wrapper


def test_a_callable_that_marks_itself_is_a_recorder() -> None:
    assert is_recorder(_recorder_over(_base, "owner"))


def test_a_plain_callable_and_none_are_not_recorders() -> None:
    assert not is_recorder(_base)
    assert not is_recorder(None)


def test_a_wrapper_that_copied_a_recorders_attributes_is_not_one() -> None:
    recorder = _recorder_over(_base, "owner")
    wrapper = _plugin_wrapper_over(recorder)
    assert getattr(wrapper, RECORDER_MARK) is recorder
    assert not is_recorder(wrapper)


def test_a_recorder_of_another_owner_is_foreign() -> None:
    recorder = _recorder_over(_base, "first")
    assert foreign_recorder(recorder, "second")
    assert not foreign_recorder(recorder, "first")


def test_only_a_recorder_can_be_foreign() -> None:
    assert not foreign_recorder(_base, "owner")
    assert not foreign_recorder(None, "owner")
    assert not foreign_recorder(_plugin_wrapper_over(_recorder_over(_base, "first")), "second")


def test_every_recorder_layer_is_stripped_down_to_the_base_callable() -> None:
    inner = _recorder_over(_base, "owner")
    outer = _recorder_over(inner, "owner")
    assert strip_recorders(outer) is _base
    assert strip_recorders(_base) is _base


def test_stripping_stops_at_a_plugin_wrapper_over_a_recorder() -> None:
    wrapper = _plugin_wrapper_over(_recorder_over(_base, "owner"))
    assert strip_recorders(wrapper) is wrapper


def test_an_uninstall_restores_what_a_recorder_wraps_and_leaves_the_rest_alone() -> None:
    recorder = _recorder_over(_base, "owner")
    assert restore_target(recorder) is _base
    assert restore_target(_base) is None
    assert restore_target(None) is None
    assert restore_target(_plugin_wrapper_over(recorder)) is None
