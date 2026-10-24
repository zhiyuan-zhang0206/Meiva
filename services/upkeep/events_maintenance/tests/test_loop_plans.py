from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from services.upkeep.events_maintenance import health_components, loop_roster, pass_policy
from services.upkeep.events_maintenance.health_components import ComponentFacts, LoopFacts
from services.upkeep.events_maintenance.pass_policy import Failure, LogLevel


def _loop(**changes: Any) -> LoopFacts:
    return replace(
        LoopFacts(
            name="dispatch",
            timeout_s=15.0,
            stale_for_s=0.0,
            wedged=False,
            last_success_at=None,
            last_error=None,
        ),
        **changes,
    )


def test_a_fresh_loop_is_ok_and_idle() -> None:
    assert health_components.component_facts(_loop()) == ComponentFacts(
        name="dispatch",
        status="ok",
        last_success=None,
        last_error=None,
        progress="idle",
        detail=None,
    )


def test_a_wedged_loop_is_degraded_with_the_reason_it_failed() -> None:
    facts = health_components.component_facts(
        _loop(wedged=True, last_error="dispatch pass exceeded hard deadline")
    )

    assert facts.status == "degraded"
    assert facts.progress == "wedged"
    assert facts.detail == "wedged: dispatch pass exceeded hard deadline"
    assert facts.last_error == "dispatch pass exceeded hard deadline"


def test_a_wedged_loop_without_a_recorded_error_names_the_deadline() -> None:
    facts = health_components.component_facts(_loop(wedged=True))

    assert facts.detail == "wedged: pass exceeded its deadline"


def test_a_loop_with_no_completed_work_past_its_timeout_is_stale() -> None:
    facts = health_components.component_facts(_loop(timeout_s=5.0, stale_for_s=6.0))

    assert facts.status == "degraded"
    assert facts.progress == "idle"
    assert facts.detail == "no progress for 6s"


def test_a_loop_exactly_at_its_timeout_is_still_ok() -> None:
    assert health_components.component_facts(_loop(timeout_s=5.0, stale_for_s=5.0)).status == "ok"


def test_the_success_stamp_becomes_epoch_seconds() -> None:
    stamped = datetime(2026, 10, 20, 9, 30, tzinfo=UTC)

    facts = health_components.component_facts(_loop(last_success_at=stamped.isoformat()))

    assert facts.last_success == stamped.timestamp()


def test_a_completed_pass_clears_the_failure_budget_and_stays_quiet() -> None:
    plan = pass_policy.verdict(None)

    assert plan.reset_budget
    assert plan.log is None
    assert not (plan.record_error or plan.count_failure or plan.park or plan.reraise)


def test_a_wedged_pass_parks_the_loop_for_the_watchdog() -> None:
    plan = pass_policy.verdict(Failure.WEDGED)

    assert plan.park and plan.log is LogLevel.CRITICAL
    assert not (plan.reraise or plan.count_failure or plan.reset_budget)


def test_schema_drift_ends_the_process_because_retrying_cannot_heal_it() -> None:
    plan = pass_policy.verdict(Failure.SCHEMA_DRIFT)

    assert plan.reraise and plan.log is LogLevel.CRITICAL
    assert not (plan.park or plan.count_failure or plan.reset_budget)


def test_an_unreachable_database_skips_the_pass_without_spending_the_budget() -> None:
    plan = pass_policy.verdict(Failure.DATABASE_UNAVAILABLE)

    assert plan.record_error and plan.log is LogLevel.WARNING
    assert not (plan.count_failure or plan.reset_budget or plan.park or plan.reraise)


def test_any_other_failure_is_remembered_and_counted() -> None:
    plan = pass_policy.verdict(Failure.OTHER)

    assert plan.record_error and plan.count_failure and plan.log is LogLevel.ERROR
    assert not (plan.reset_budget or plan.park or plan.reraise)


@pytest.mark.parametrize("failure", list(Failure))
def test_every_failure_has_a_verdict_that_ends_in_exactly_one_way(failure: Failure) -> None:
    plan = pass_policy.verdict(failure)

    assert [plan.park, plan.reraise].count(True) <= 1


def test_a_unit_without_the_grafana_credential_runs_three_loops() -> None:
    loops = loop_roster.roster(
        dispatch_deadline_s=1500.0, resolution_deadline_s=600.0, reconcile_alerts=False
    )

    assert [(loop.name, loop.deadline_s) for loop in loops] == [
        ("dispatch", 1500.0),
        ("resolution", 600.0),
        ("registry_gauge", loop_roster.REGISTRY_GAUGE_DEADLINE_S),
    ]


def test_a_unit_with_the_credential_adds_the_alert_reconciliation() -> None:
    loops = loop_roster.roster(
        dispatch_deadline_s=1500.0, resolution_deadline_s=600.0, reconcile_alerts=True
    )

    assert [loop.name for loop in loops] == [
        "dispatch",
        "resolution",
        "registry_gauge",
        "alert_reconciliation",
    ]
    assert loops[-1].deadline_s == loop_roster.ALERT_RECONCILIATION_DEADLINE_S


def test_the_configured_deadlines_reach_the_blocking_loops_only() -> None:
    loops = loop_roster.roster(
        dispatch_deadline_s=11.0, resolution_deadline_s=22.0, reconcile_alerts=True
    )

    assert {loop.name: loop.deadline_s for loop in loops}["dispatch"] == 11.0
    assert {loop.name: loop.deadline_s for loop in loops}["resolution"] == 22.0
