# meiva:note
# What a start resumes, refuses or reports, and what a stop takes down, from one snapshot.

from __future__ import annotations

import pytest

from cli.commands.lifecycle.root_control.reconcile_plan import (
    LeaveTree,
    ResumePlan,
    StopUnits,
    UnitProblem,
    UnitResume,
    changed_inputs,
    classify_launch,
    generation_refusal,
    plan_resume,
    plan_tree_stop,
)
from cli.commands.lifecycle.tests.root_control.snapshots import snapshot_of
from cli.commands.lifecycle.tests.scripted_root import UnitRow


def test_an_operator_start_brings_up_every_stopped_unit_in_roster_order() -> None:
    snapshot = snapshot_of(
        UnitRow("gateway", state="running"),
        UnitRow("frontend", state="stopped"),
        UnitRow("ops", state="stopped"),
    )

    plan = plan_resume(["ops", "gateway", "frontend"], snapshot, honor_restart_budget=False)

    assert plan == ResumePlan(
        up=(UnitResume("ops", "stopped"), UnitResume("frontend", "stopped")), held_down=()
    )


def test_a_running_unit_whose_intent_is_stopped_is_resumed_too() -> None:
    snapshot = snapshot_of(UnitRow("gateway", state="running", intent="stopped"))

    plan = plan_resume(["gateway"], snapshot, honor_restart_budget=False)

    assert plan.up == (UnitResume("gateway", "running"),)


def test_the_reason_is_the_last_error_then_the_last_exit_then_the_state() -> None:
    snapshot = snapshot_of(
        UnitRow("a", state="stopped", last_error="unexpected exit: signal 9"),
        UnitRow("b", state="stopped"),
    )

    plan = plan_resume(["a", "b"], snapshot, honor_restart_budget=False)

    assert [resume.reason for resume in plan.up] == ["unexpected exit: signal 9", "stopped"]


def test_an_explicit_start_revives_a_unit_whose_restart_budget_is_spent() -> None:
    snapshot = snapshot_of(UnitRow("gateway", state="stopped", restart_limit_reached=True))

    plan = plan_resume(["gateway"], snapshot, honor_restart_budget=False)

    assert plan == ResumePlan(up=(UnitResume("gateway", "stopped"),), held_down=())


def test_a_boot_attempt_leaves_a_unit_down_whose_restart_budget_is_spent() -> None:
    # meiva:note
    # The boot job's retried start must not race the health monitor's ladder.
    snapshot = snapshot_of(UnitRow("gateway", state="stopped", restart_limit_reached=True))

    plan = plan_resume(["gateway"], snapshot, honor_restart_budget=True)

    assert plan == ResumePlan(up=(), held_down=("gateway",))


def test_a_boot_attempt_still_brings_up_a_unit_with_budget_left() -> None:
    snapshot = snapshot_of(UnitRow("gateway", state="stopped", restart_limit_reached=False))

    plan = plan_resume(["gateway"], snapshot, honor_restart_budget=True)

    assert plan.up == (UnitResume("gateway", "stopped"),)


def test_resuming_a_unit_the_root_does_not_run_is_refused() -> None:
    with pytest.raises(ValueError, match="no unit 'ops'"):
        plan_resume(["ops"], snapshot_of(UnitRow("gateway")), honor_restart_budget=False)


def test_launch_problems_are_the_missing_units_and_the_stopped_ones_with_an_error() -> None:
    snapshot = snapshot_of(
        UnitRow("gateway", state="stopped", last_error="unexpected exit: 1"),
        UnitRow("frontend", state="stopped"),
        UnitRow("ops", state="running", last_error="old error"),
    )

    problems = classify_launch(["gateway", "frontend", "ops", "browser"], snapshot)

    assert problems == (
        UnitProblem("gateway", "unexpected exit: 1"),
        UnitProblem("browser", "missing from the meiva-root tree"),
    )


def test_the_same_inputs_change_nothing() -> None:
    snapshot = snapshot_of(
        UnitRow("gateway", manifest_digest="m1"),
        UnitRow("ops", manifest_digest="m2"),
        launch_digest="L",
    )

    assert changed_inputs({"gateway": "m1", "ops": "m2"}, "L", snapshot, reconcile=True) == ()


def test_a_unit_with_another_manifest_digest_or_none_at_all_is_changed() -> None:
    snapshot = snapshot_of(UnitRow("gateway", manifest_digest="old"), launch_digest="L")

    changed = changed_inputs({"gateway": "new", "ops": "m2"}, "L", snapshot, reconcile=False)

    assert changed == ("gateway", "ops")


def test_a_root_serving_other_launch_inputs_is_changed_whatever_its_units_say() -> None:
    snapshot = snapshot_of(UnitRow("gateway", manifest_digest="m1"), launch_digest="old")

    assert changed_inputs({"gateway": "m1"}, "new", snapshot, reconcile=False) == (
        "root launch inputs",
    )


def test_a_root_launched_without_a_digest_never_matches_a_requested_one() -> None:
    snapshot = snapshot_of(UnitRow("gateway", manifest_digest="m1"))

    assert changed_inputs({"gateway": "m1"}, "L", snapshot, reconcile=False) == (
        "root launch inputs",
    )


def test_reconciling_the_whole_tree_also_names_the_units_beyond_the_request() -> None:
    snapshot = snapshot_of(
        UnitRow("gateway", manifest_digest="m1"),
        UnitRow("agent-host", manifest_digest="m3"),
        launch_digest="L",
    )

    assert changed_inputs({"gateway": "m1"}, "L", snapshot, reconcile=True) == ("agent-host",)
    assert changed_inputs({"gateway": "m1"}, "L", snapshot, reconcile=False) == ()


def test_the_refusal_names_every_change_and_the_stop_that_resolves_it() -> None:
    assert generation_refusal(["ops", "gateway"]) == (
        "requested services change the immutable root generation: gateway, ops; "
        "run meiva stop before starting with changed services or configuration"
    )


def test_a_stop_without_a_preserved_unit_takes_the_whole_tree_and_the_root() -> None:
    plan = plan_tree_stop(["agent-host", "gateway", "ops"], frozenset())

    assert plan == StopUnits(("agent-host", "gateway", "ops"), stop_root=True)


def test_preserved_units_stay_and_keep_the_root_running_to_host_them() -> None:
    plan = plan_tree_stop(["agent-host", "gateway", "ops"], frozenset({"gateway"}))

    assert plan == StopUnits(("agent-host", "ops"), stop_root=False)


def test_a_stop_that_preserves_every_unit_changes_nothing() -> None:
    assert plan_tree_stop(["gateway", "ops"], frozenset({"ops", "gateway", "gone"})) == LeaveTree()


def test_preserving_a_unit_the_root_does_not_run_does_not_keep_the_root() -> None:
    assert plan_tree_stop(["gateway"], frozenset({"gone"})) == StopUnits(
        ("gateway",), stop_root=True
    )


def test_a_root_hosting_no_unit_is_stopped_too() -> None:
    assert plan_tree_stop([], frozenset()) == StopUnits((), stop_root=True)
