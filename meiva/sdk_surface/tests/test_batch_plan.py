# meiva:note
# `meiva/sdk_surface/pure/plans/batch_plan.py` unit tests: the `max_concurrent` contract every SDK
# batch API shares, asserted by value.

from __future__ import annotations

from typing import Any

import pytest

from meiva.sdk_surface.pure.plans.batch_plan import (
    DEFAULT_BATCH_MAX_CONCURRENT,
    validate_max_concurrent,
)


# meiva:note
# SDK batch entry points default to a bounded, caller-overridable cap.
def test_default_batch_cap_is_a_safe_configurable_ceiling() -> None:
    assert 8 <= DEFAULT_BATCH_MAX_CONCURRENT <= 16


def test_validate_max_concurrent_accepts_valid_values() -> None:
    validate_max_concurrent(None, example="x")
    validate_max_concurrent(1, example="x")
    validate_max_concurrent(4, example="x")


# meiva:note
# The shared knob keeps the per-API messages: bad values fail fast with the fragments the
# entry-point tests assert ("at least 1" / "int or None").
@pytest.mark.parametrize("bad", [0, -3])
def test_validate_max_concurrent_rejects_a_cap_below_one(bad: int) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        validate_max_concurrent(bad, example="x")


# meiva:note
# A float, a string and a bool are all refused: the bool is an int subclass, but `True` as a
# concurrency cap is a bug. The value is typed `Any` because the type checker refuses these by
# construction.
@pytest.mark.parametrize("bad", [2.5, "4", True])
def test_validate_max_concurrent_rejects_a_non_int_cap(bad: Any) -> None:
    with pytest.raises(TypeError, match="int or None"):
        validate_max_concurrent(bad, example="x")


def test_validate_max_concurrent_message_names_the_calling_api() -> None:
    bad: Any = 2.5
    with pytest.raises(TypeError, match=r"meiva\.web\.fetch\(targets, max_concurrent=4\)"):
        validate_max_concurrent(bad, example="meiva.web.fetch(targets, max_concurrent=4)")
