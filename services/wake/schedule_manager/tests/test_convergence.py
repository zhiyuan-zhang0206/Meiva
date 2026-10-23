from __future__ import annotations

import pytest

from services.wake.schedule_manager.convergence import (
    Desired,
    Prepare,
    Reclaim,
    SyncAction,
    launch_command,
    plan_sync,
    prepare_verdict,
    reclaim_verdict,
    revision_in,
)


def _wanted(
    *,
    enabled: bool = True,
    status: str = "stopped",
    revision: int = 1,
    applied: int = 0,
    generation: str | None = None,
) -> Desired:
    return Desired(enabled, status, revision, applied, generation)


def test_a_deleted_schedule_has_its_session_reaped() -> None:
    assert plan_sync(None, live=True, observed=1) is SyncAction.REAP_UNWANTED
    assert plan_sync(None, live=False, observed=None) is SyncAction.REAP_UNWANTED


def test_a_disabled_schedule_has_its_session_reaped() -> None:
    assert plan_sync(_wanted(enabled=False), live=True, observed=1) is SyncAction.REAP_UNWANTED


def test_a_live_session_of_the_wanted_revision_is_adopted() -> None:
    assert plan_sync(_wanted(revision=2), live=True, observed=2) is SyncAction.ADOPT_CURRENT


def test_a_live_session_of_another_revision_is_replaced() -> None:
    assert plan_sync(_wanted(revision=3), live=True, observed=2) is SyncAction.REPLACE_STALE


def test_a_legacy_session_without_provenance_is_adopted() -> None:
    wanted = _wanted(revision=0, applied=0, status="running")

    assert plan_sync(wanted, live=True, observed=None) is SyncAction.ADOPT_LEGACY


def test_a_versioned_session_without_provenance_is_held_not_killed() -> None:
    assert plan_sync(_wanted(revision=1), live=True, observed=None) is SyncAction.HOLD_UNREADABLE
    wanted = _wanted(revision=0, applied=1, status="running")
    assert plan_sync(wanted, live=True, observed=None) is SyncAction.HOLD_UNREADABLE


@pytest.mark.parametrize("status", ["completed", "error"])
def test_an_admitted_revision_that_ended_is_settled_not_relaunched(status: str) -> None:
    wanted = _wanted(revision=2, applied=2, status=status)

    assert plan_sync(wanted, live=False, observed=None) is SyncAction.SETTLED


def test_an_admitted_revision_marked_running_with_no_session_is_reclaimed_or_lost() -> None:
    wanted = _wanted(revision=2, applied=2, status="running")

    assert plan_sync(wanted, live=False, observed=None) is SyncAction.RECLAIM_RUNNING


@pytest.mark.parametrize("status", ["stopped", "running", "completed", "error"])
def test_a_new_revision_with_no_session_is_launched(status: str) -> None:
    assert plan_sync(_wanted(revision=2, applied=1, status=status), live=False, observed=None) is (
        SyncAction.LAUNCH
    )


def test_a_launch_with_no_survivor_is_clear() -> None:
    verdict = prepare_verdict(_wanted(), present=False, current_generation=False, observed=None)

    assert verdict is Prepare.CLEAR


def test_a_survivor_already_running_the_wanted_revision_is_adopted() -> None:
    verdict = prepare_verdict(
        _wanted(revision=2), present=True, current_generation=True, observed=2
    )

    assert verdict is Prepare.ADOPT


def test_a_survivor_of_another_revision_is_reaped_first() -> None:
    verdict = prepare_verdict(
        _wanted(revision=3), present=True, current_generation=True, observed=2
    )

    assert verdict is Prepare.REAP


def test_a_survivor_of_an_older_allocation_generation_is_reaped_without_asking_its_provenance() -> (
    None
):
    verdict = prepare_verdict(
        _wanted(revision=3), present=True, current_generation=False, observed=None
    )

    assert verdict is Prepare.REAP


def test_a_survivor_with_unreadable_provenance_is_held_for_a_versioned_revision() -> None:
    verdict = prepare_verdict(
        _wanted(revision=1), present=True, current_generation=True, observed=None
    )

    assert verdict is Prepare.HOLD


def test_a_healthy_legacy_survivor_is_held() -> None:
    wanted = _wanted(revision=0, applied=0, status="running")

    assert (
        prepare_verdict(wanted, present=True, current_generation=True, observed=None)
        is Prepare.HOLD
    )


def test_a_stale_legacy_survivor_is_reaped() -> None:
    wanted = _wanted(revision=0, applied=0, status="stopped")

    assert (
        prepare_verdict(wanted, present=True, current_generation=True, observed=None)
        is Prepare.REAP
    )


@pytest.mark.parametrize(
    ("generation", "launched_in"),
    [(None, "host-a"), ("host-b", None), ("host-a", "host-a")],
    ids=["host-silent", "launch-predates-generations", "same-host"],
)
def test_without_a_replaced_host_nothing_is_reclaimed(
    generation: str | None, launched_in: str | None
) -> None:
    verdict = reclaim_verdict(
        generation=generation, launched_in=launched_in, replay=False, attempts=0, limit=5
    )

    assert verdict is Reclaim.NOT_REPLACED


def test_a_replaced_host_authorizes_one_relaunch_inside_the_budget() -> None:
    verdict = reclaim_verdict(
        generation="host-b", launched_in="host-a", replay=False, attempts=4, limit=5
    )

    assert verdict is Reclaim.AUTHORIZE


def test_a_spent_budget_suppresses_a_new_relaunch_but_not_a_replay() -> None:
    new = reclaim_verdict(
        generation="host-b", launched_in="host-a", replay=False, attempts=5, limit=5
    )
    replay = reclaim_verdict(
        generation="host-b", launched_in="host-a", replay=True, attempts=5, limit=5
    )

    assert new is Reclaim.SUPPRESSED
    assert replay is Reclaim.AUTHORIZE


def test_the_launch_command_changes_into_the_checkout_and_ends_with_its_runner() -> None:
    command = launch_command("/srv/my checkout", 12, 3)

    assert command == (
        "cd '/srv/my checkout' && "
        ".venv/bin/python -m services.wake.schedule_manager.runner 12 3; exit $?"
    )


def test_a_launched_revision_is_read_back_from_that_command() -> None:
    command = launch_command("/repo", 12, 3)

    assert revision_in(command) == 3


def test_a_legacy_command_belongs_to_revision_zero_and_no_command_to_none() -> None:
    legacy = "cd /repo && .venv/bin/python -m services.wake.schedule_manager.runner 12; exit $?"

    assert revision_in(legacy) == 0
    assert revision_in(None) is None
