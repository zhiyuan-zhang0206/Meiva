# meiva:note
# What `meiva restart` refuses, runs and returns, asserted on the plan; no host involved.

from __future__ import annotations

from dataclasses import replace

import pytest

from base.host.exit_codes import RESTART_DECLINED_EXIT_CODE
from cli.commands.lifecycle.stop.restart_plan import (
    AGENT_HOST,
    NO_ROOT_REFUSAL,
    Continue,
    Finish,
    Leg,
    RestartRequest,
    StartLeg,
    budget_line,
    missing_unit_refusal,
    restart_refusal,
    restart_start_leg,
    restart_stop_request,
    settle_leg,
)


def test_a_restart_inside_an_exec_domain_is_declined_before_anything_stops() -> None:
    # meiva:note
    # The execute_code call's teardown SIGKILLs its process group, this restart with it.
    refusal = restart_refusal("agent.execution.child", None)
    assert refusal is not None
    assert "(agent.execution.child)" in refusal.message and "refusing restart" in refusal.message
    assert "plain ssh/login shell" in refusal.message


def test_a_restart_inside_a_supervised_session_is_declined_because_its_stop_leg_kills_it() -> None:
    refusal = restart_refusal(None, "meiva-agent-host")
    assert refusal is not None
    assert "'meiva-agent-host'" in refusal.message and "host still serving" in refusal.message


def test_the_exec_domain_is_named_when_both_host_the_restart() -> None:
    both = restart_refusal("domain", "session")
    assert both is not None and "execute_code" in both.message


def test_a_restart_hosted_by_nothing_is_not_refused() -> None:
    assert restart_refusal(None, None) is None


BASE = RestartRequest(
    only_services=(),
    disabled_services=(),
    all_services=True,
    keep_shell_sessions=False,
    keep_browser=True,
    restart_extras=False,
    force=False,
    timeout=30.0,
    start_timeout=20.0,
)


@pytest.mark.parametrize(
    ("roles", "keep_infra"), [({"gateway", "agent-runner"}, True), ({"agent-runner"}, None)]
)
def test_the_stop_leg_keeps_the_data_plane_only_where_there_is_one(
    roles: set[str], keep_infra: bool | None
) -> None:
    stop = restart_stop_request(BASE, roles=frozenset(roles))
    assert stop.choices.keep_infra is keep_infra


def test_the_stop_leg_forwards_the_operators_choices_and_adds_none_of_its_own() -> None:
    chosen = replace(
        BASE,
        keep_browser=False,
        keep_shell_sessions=True,
        restart_extras=True,
        force=True,
        timeout=42.0,
        message="Zhiyuan restarting host",
    )
    stop = restart_stop_request(chosen, roles=frozenset({"gateway"}))
    choices = stop.choices
    assert (choices.keep_browser, choices.keep_shell_sessions) == (False, True)
    assert (choices.teardown_extras, choices.force) == (True, True)
    assert choices.message == "Zhiyuan restarting host"
    assert choices.preserve_sessions == frozenset(), "a restart preserves no extra service"
    assert choices.announce is False, "a restart announces no stop: the host comes straight back"
    assert stop.timeout == 42.0, "the stop leg is bounded by --timeout, not --start-timeout"


# meiva:note
# It runs in a non-interactive context too (the detached-updater path), like `meiva start`:
# it needs no tty.
def test_a_restart_never_asks_for_a_confirmation() -> None:
    stop = restart_stop_request(BASE, roles=frozenset({"gateway"}))
    assert stop.require_confirmation is False
    assert stop.window.for_seconds is None and stop.window.reason is None


def test_the_start_leg_launches_the_operators_selection_and_does_not_reconcile_the_kept_units() -> (
    None
):
    chosen = replace(
        BASE, only_services=("agent-host",), disabled_services=("browser",), all_services=False
    )
    launch = restart_start_leg(chosen)
    assert launch == StartLeg(
        only_services=("agent-host",),
        disabled_services=("browser",),
        all_services=False,
        reconcile=False,
        ready_timeout=20.0,
    )


def test_the_legs_run_in_the_order_that_validates_before_it_kills() -> None:
    assert list(Leg) == [Leg.PROBES, Leg.READINESS, Leg.STOP, Leg.START]
    assert [leg.phase for leg in Leg] == ["preflight", "preflight", "stop", "start"]
    assert Leg.PROBES.heading == "\n→ preflight probes (validate-before-kill)"
    assert "start readiness preflight" in Leg.READINESS.heading
    assert Leg.STOP.heading == Leg.START.heading == ""


@pytest.mark.parametrize("leg", [Leg.PROBES, Leg.READINESS, Leg.STOP])
def test_a_leg_that_passes_lets_the_restart_go_on(leg: Leg) -> None:
    assert settle_leg(leg, 0) == Continue()


# meiva:note
# A refusal stops nothing, so the host is still serving. It must be distinguishable from a
# failure after the stop: the updater shell branches on exactly this code to decide whether
# to run `meiva start`.
@pytest.mark.parametrize(
    ("leg", "error"),
    [
        (Leg.PROBES, "preflight probes failed"),
        (Leg.READINESS, "start-readiness preflight failed"),
    ],
)
def test_a_refusal_before_the_stop_declines_with_its_own_exit_code(leg: Leg, error: str) -> None:
    for rc in (1, 7):
        outcome = settle_leg(leg, rc)
        assert isinstance(outcome, Finish)
        assert (outcome.rc, outcome.error) == (RESTART_DECLINED_EXIT_CODE, error)
        assert outcome.notice is not None and "host still serving" in outcome.notice
        assert outcome.notice.startswith("  ✗ refusing restart:")


def test_a_failed_stop_leg_is_the_restarts_result_and_no_start_follows() -> None:
    assert settle_leg(Leg.STOP, 1) == Finish(rc=1, error="stop leg failed", notice=None)
    assert settle_leg(Leg.STOP, 5) == Finish(rc=5, error="stop leg failed", notice=None)


# meiva:note
# Once the stop has happened the host may be DOWN, so its code must NOT be the one the
# updater treats as "still serving".
def test_the_start_leg_ends_the_restart_and_a_failure_after_the_stop_is_not_a_decline() -> None:
    assert settle_leg(Leg.START, 0) == Finish(rc=0, error=None, notice=None)
    failed = settle_leg(Leg.START, 1)
    assert failed == Finish(rc=1, error="start leg failed with rc=1", notice=None)
    assert isinstance(failed, Finish) and failed.rc != RESTART_DECLINED_EXIT_CODE


def test_the_budget_line_states_what_an_outer_caller_must_outlast() -> None:
    line = budget_line(42.0, 30.0, "/home/run/lifecycle-op.json")
    assert line.startswith("[meiva restart] budget contract: stop up to 42s + start readiness")
    assert "up to 30s" in line and "/home/run/lifecycle-op.json" in line


def test_restarting_agent_host_needs_a_live_root_that_runs_it() -> None:
    assert "no live meiva-root" in NO_ROOT_REFUSAL and "meiva start" in NO_ROOT_REFUSAL
    assert missing_unit_refusal((AGENT_HOST, "gateway")) is None
    refusal = missing_unit_refusal(("gateway",))
    assert refusal is not None and f"runs no {AGENT_HOST} unit" in refusal
