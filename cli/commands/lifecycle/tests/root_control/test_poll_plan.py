# meiva:note
# What each wait on the root, its keeper or a service does next, from one observation.

from __future__ import annotations

import pytest

from base.host.permissions_helper.wire import RootStatus
from cli.commands.lifecycle.root_control.poll_plan import (
    Expired,
    KeeperPending,
    KeeperRefused,
    KeeperRunning,
    ReadinessRound,
    Retry,
    keeper_verdict,
    observe_readiness,
    unit_ready,
    unmet_step,
)
from cli.commands.lifecycle.root_control.snapshot import root_units
from cli.commands.lifecycle.tests.root_control.snapshots import snapshot_of
from cli.commands.lifecycle.tests.scripted_root import UnitRow


def test_an_unmet_wait_inside_its_budget_observes_again_after_the_interval() -> None:
    assert unmet_step(elapsed_s=0.0, timeout_s=10.0, interval_s=0.5) == Retry(0.5)
    assert unmet_step(elapsed_s=9.99, timeout_s=10.0, interval_s=0.5) == Retry(0.5)


@pytest.mark.parametrize("elapsed_s", [10.0, 10.5, 99.0])
def test_an_unmet_wait_past_its_budget_expires(elapsed_s: float) -> None:
    assert unmet_step(elapsed_s=elapsed_s, timeout_s=10.0, interval_s=0.5) == Expired()


def test_a_spent_budget_expires_after_the_one_observation_every_wait_makes() -> None:
    assert unmet_step(elapsed_s=0.0, timeout_s=0.0, interval_s=0.5) == Expired()


def keeper(state: str) -> RootStatus:
    return {"state": state, "seeded": True, "restarts": 0, "stop_requested": False}


def test_a_running_keeper_has_brought_the_root_up() -> None:
    assert keeper_verdict(keeper("running"), "/run") == KeeperRunning()


@pytest.mark.parametrize("state", ["backoff", "stopping", "unseeded"])
def test_a_keeper_still_settling_is_waited_on(state: str) -> None:
    assert keeper_verdict(keeper(state), "/run") == KeeperPending()


def test_a_keeper_in_conflict_names_the_run_dir_it_will_not_spawn_into() -> None:
    verdict = keeper_verdict(keeper("conflict"), "/home/run")

    assert isinstance(verdict, KeeperRefused)
    assert "another meiva-root owns /home/run" in verdict.reason
    assert "resolve the conflict first" in verdict.reason


def test_a_keeper_that_gave_up_restarting_reports_how_the_root_last_exited() -> None:
    exited: RootStatus = {
        **keeper("exited"),
        "last_exit": {"kind": "crash", "code": 1, "at": 0.0},
    }

    verdict = keeper_verdict(exited, "/run")

    assert isinstance(verdict, KeeperRefused)
    assert "stopped restarting a root that keeps exiting" in verdict.reason
    assert "'kind': 'crash'" in verdict.reason


def test_a_rejected_seed_is_reported_even_while_the_state_is_not_final() -> None:
    rejected: RootStatus = {**keeper("unseeded"), "seed_error": "bad argv"}

    verdict = keeper_verdict(rejected, "/run")

    assert verdict == KeeperRefused("the root keeper rejected the seed: bad argv")


@pytest.mark.parametrize("verdict", [None, "unavailable", "down", "port-taken"])
def test_no_missing_or_failed_health_is_ready(verdict: str | None) -> None:
    unit = root_units(snapshot_of(UnitRow("gateway")))["gateway"]

    assert not unit_ready(unit, verdict)


def test_a_running_unit_with_a_positive_protocol_response_is_ready() -> None:
    unit = root_units(snapshot_of(UnitRow("gateway")))["gateway"]

    assert unit_ready(unit, "alive")
    assert not unit_ready(None, "alive")


def test_a_stopped_unit_is_not_ready_even_if_its_endpoint_answers() -> None:
    unit = root_units(snapshot_of(UnitRow("gateway", state="stopped")))["gateway"]

    assert not unit_ready(unit, "alive")


def observe(
    *rows: UnitRow, verdicts: dict[str, str], recorded: frozenset[str] = frozenset()
) -> ReadinessRound:
    units = root_units(snapshot_of(*rows))
    return observe_readiness([row.unit_id for row in rows], units, verdicts, recorded)


def test_a_ready_core_settles_and_the_ready_services_are_reported_once() -> None:
    first = observe(
        UnitRow("gateway"), UnitRow("frontend"), verdicts={"gateway": "alive", "frontend": "alive"}
    )
    again = observe(
        UnitRow("gateway"),
        UnitRow("frontend"),
        verdicts={"gateway": "alive", "frontend": "alive"},
        recorded=frozenset({"gateway"}),
    )

    assert first.settled and first.unready == ()
    assert first.newly_ready == ("gateway", "frontend")
    assert again.newly_ready == ("frontend",)


def test_an_unready_optional_service_is_a_diagnostic_and_never_delays_a_ready_core() -> None:
    round_ = observe(
        UnitRow("gateway"),
        UnitRow("browser-mcp", state="stopped"),
        verdicts={"gateway": "alive", "browser-mcp": "down"},
    )

    assert round_.settled
    assert round_.unready == ()
    assert round_.non_critical_unready == ("browser-mcp",)
    assert not round_.sessions_gone


def test_a_core_unit_still_starting_keeps_the_wait_open() -> None:
    round_ = observe(UnitRow("gateway"), verdicts={"gateway": "down"})

    assert not round_.settled
    assert round_.unready == ("gateway",)
    assert not round_.sessions_gone


def test_a_wait_whose_core_units_are_all_stopped_ends_early_as_gone() -> None:
    round_ = observe(
        UnitRow("gateway", state="stopped"),
        UnitRow("frontend", state="stopped"),
        verdicts={"gateway": "alive"},
    )

    assert round_.settled and round_.sessions_gone
    assert round_.unready == ("gateway", "frontend")


def test_one_live_unready_core_unit_keeps_the_gone_verdict_out() -> None:
    round_ = observe(
        UnitRow("gateway", state="stopped"), UnitRow("frontend"), verdicts={"frontend": "down"}
    )

    assert not round_.settled and not round_.sessions_gone


def test_a_requested_unit_the_root_does_not_list_is_unready_not_gone() -> None:
    round_ = observe_readiness(["gateway"], {}, {}, frozenset())

    assert round_.unready == ("gateway",)
    assert not round_.sessions_gone
