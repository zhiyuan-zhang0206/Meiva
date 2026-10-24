from __future__ import annotations

import pytest

from ops.upload_delivery.copy_plan import (
    Admission,
    FetchObject,
    ReuseObject,
    admission,
    fetch_timeout_s,
    plan_objects,
)


# meiva:note
# No reservation row means a new reservation, whatever the rest of the request looks like.
def test_a_missing_row_admits_a_new_reservation() -> None:
    assert admission(row_exists=False, same_request=False, same_directory=False) is Admission.NEW


# meiva:note
# A caller that claims a stored request matches while it found no row has a bug; the plan
# refuses to guess.
@pytest.mark.parametrize(("same_request", "same_directory"), [(True, False), (False, True)])
def test_facts_about_a_row_that_does_not_exist_are_refused(
    same_request: bool, same_directory: bool
) -> None:
    with pytest.raises(ValueError, match="missing reservation row"):
        admission(row_exists=False, same_request=same_request, same_directory=same_directory)


# meiva:note
# The reservation a request already made resumes: its ready marker is historical, so the
# shell checks the quota again before it refills.
def test_the_same_request_on_the_same_root_resumes() -> None:
    assert admission(row_exists=True, same_request=True, same_directory=True) is Admission.RESUME


# meiva:note
# A batch id that already binds another manifest or another native root is a conflict, and
# stays one when both differ.
@pytest.mark.parametrize(
    ("same_request", "same_directory"), [(False, True), (True, False), (False, False)]
)
def test_a_batch_bound_to_another_manifest_or_root_conflicts(
    same_request: bool, same_directory: bool
) -> None:
    assert (
        admission(row_exists=True, same_request=same_request, same_directory=same_directory)
        is Admission.CONFLICT
    )


# meiva:note
# Present files are reused and absent ones fetched, one step per object in manifest order.
def test_objects_that_exist_are_reused_and_the_rest_are_fetched_in_order() -> None:
    batch = "b" * 32

    steps = plan_objects((True, False, False, True), agent_id=7, batch_id=batch)

    assert steps == (
        ReuseObject(0),
        FetchObject(1, f"/api/keyed/v1/agents/7/uploads/{batch}/objects/1"),
        FetchObject(2, f"/api/keyed/v1/agents/7/uploads/{batch}/objects/2"),
        ReuseObject(3),
    )


# meiva:note
# A request waits no longer than its cap, nor longer than the batch's round has left.
@pytest.mark.parametrize(
    ("remaining_s", "cap_s", "expected"),
    [(120.0, 30.0, 30.0), (12.5, 30.0, 12.5), (30.0, 30.0, 30.0)],
)
def test_a_fetch_waits_for_the_smaller_of_its_cap_and_the_time_left(
    remaining_s: float, cap_s: float, expected: float
) -> None:
    assert fetch_timeout_s(remaining_s=remaining_s, request_cap_s=cap_s) == expected


# meiva:note
# A spent round yields no timeout at all, so the shell refuses to dial instead of waiting 0 s.
@pytest.mark.parametrize("remaining_s", [0.0, -0.001, -60.0])
def test_a_spent_round_yields_no_timeout(remaining_s: float) -> None:
    assert fetch_timeout_s(remaining_s=remaining_s, request_cap_s=30.0) is None


# meiva:note
# A cap that is not positive is a configuration error, not a round that is spent.
@pytest.mark.parametrize("cap_s", [0.0, -1.0])
def test_a_non_positive_cap_is_refused(cap_s: float) -> None:
    with pytest.raises(ValueError, match="positive timeout cap"):
        fetch_timeout_s(remaining_s=10.0, request_cap_s=cap_s)
