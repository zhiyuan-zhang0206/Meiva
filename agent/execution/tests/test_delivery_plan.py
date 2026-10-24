from typing import cast, get_args

import pytest

from agent.execution.delivery_plan import (
    FinalizePlan,
    delivers_telemetry,
    plan_finalize,
    rewrites_result_after_delivery_failure,
)
from agent.execution.result_kind import ResultKind


@pytest.mark.parametrize(
    ("kind", "delivers", "rewrites"),
    [
        ("done", True, True),
        ("lifecycle", True, True),
        ("crashed", True, False),
        ("cancelled", False, False),
        ("timed_out", False, False),
    ],
)
def test_a_failed_delivery_rewrites_a_success_but_never_replaces_a_crash(
    kind: ResultKind, delivers: bool, rewrites: bool
) -> None:
    assert delivers_telemetry(kind) is delivers
    assert rewrites_result_after_delivery_failure(kind) is rewrites


def test_every_result_kind_has_a_delivery_decision() -> None:
    for kind in get_args(ResultKind):
        delivers_telemetry(kind)
        rewrites_result_after_delivery_failure(kind)


def test_an_unknown_kind_does_not_deliver_telemetry() -> None:
    with pytest.raises(ValueError, match="unknown result kind 'exploded'"):
        delivers_telemetry(cast(ResultKind, "exploded"))


def test_an_unknown_kind_does_not_decide_a_rewrite() -> None:
    with pytest.raises(ValueError, match="unknown result kind 'exploded'"):
        rewrites_result_after_delivery_failure(cast(ResultKind, "exploded"))


@pytest.mark.parametrize(
    ("owned_unfinished", "ordinary_unfinished", "plan"),
    [
        (None, False, FinalizePlan(False, False, False)),
        (None, True, FinalizePlan(False, True, False)),
        (False, False, FinalizePlan(False, False, True)),
        (False, True, FinalizePlan(False, True, True)),
        (True, False, FinalizePlan(True, False, True)),
        (True, True, FinalizePlan(True, True, True)),
    ],
)
def test_an_unfinished_drain_only_warns_and_the_owned_exporter_is_still_finalized(
    owned_unfinished: bool | None, ordinary_unfinished: bool, plan: FinalizePlan
) -> None:
    assert (
        plan_finalize(owned_unfinished=owned_unfinished, ordinary_unfinished=ordinary_unfinished)
        == plan
    )
