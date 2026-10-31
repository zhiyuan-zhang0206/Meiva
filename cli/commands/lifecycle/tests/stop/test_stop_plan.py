# meiva:note
# What a stop does, in what order and what it keeps: asserted on the plan, no host involved.
#
# Every keep-or-stop choice is the operator's; the steps a stop takes follow only from them and
# from the host facts handed in. A contradictory combination is refused by the plan, before the
# shell has signalled a unit.

from __future__ import annotations

from dataclasses import replace

import pytest

from cli.commands.lifecycle.stop.choices import StopChoices, StopHost, StopRoster, StopWindow
from cli.commands.lifecycle.stop.plan import plan_stop, require_consistent
from cli.commands.lifecycle.stop.restart_plan import RestartRequest, restart_stop_request
from cli.commands.lifecycle.stop.steps import (
    Announce,
    ClearServing,
    CloseShellSessions,
    DataPlane,
    ReapChrome,
    RecordBrowserSession,
    SkipDataPlane,
    StopBrowser,
    StopDataPlane,
    StopExtras,
    StopServices,
    phase_label,
)

PTY = "pty-sessions"
RUNNER = StopHost(
    roles=frozenset({"agent-runner"}),
    roster=StopRoster(
        known=frozenset({"agent-host", "browser", "frontend", "gateway", PTY}),
        requires_db=frozenset({"agent-host", "gateway"}),
    ),
    pty_unit=PTY,
    unstarted=False,
)
GATEWAY = replace(RUNNER, roles=frozenset({"gateway", "agent-runner"}))
WINDOW = StopWindow(for_seconds=None, reason=None)


def choices(
    *,
    keep_infra: bool | None = None,
    preserve: frozenset[str] = frozenset(),
    keep_browser: bool = True,
    keep_shell_sessions: bool = False,
    announce: bool = False,
    teardown_extras: bool = False,
    force: bool = False,
    message: str | None = None,
) -> StopChoices:
    return StopChoices(
        keep_infra=keep_infra,
        preserve_sessions=preserve,
        keep_browser=keep_browser,
        keep_shell_sessions=keep_shell_sessions,
        announce=announce,
        teardown_extras=teardown_extras,
        force=force,
        message=message,
    )


def restart_request(
    *, keep_browser: bool = True, keep_shell_sessions: bool = False, restart_extras: bool = False
) -> RestartRequest:
    return RestartRequest(
        only_services=(),
        disabled_services=(),
        all_services=True,
        keep_shell_sessions=keep_shell_sessions,
        keep_browser=keep_browser,
        restart_extras=restart_extras,
        force=False,
        timeout=30.0,
        start_timeout=30.0,
    )


def steps(chosen: StopChoices, host: StopHost = RUNNER, window: StopWindow = WINDOW):
    return plan_stop(chosen, host, window).steps


def services(
    *preserve: str,
    force: bool = False,
    phase: str = "services",
    message: str | None = None,
) -> StopServices:
    return StopServices(frozenset(preserve), force=force, phase=phase, message=message)


# meiva:note
# ── a normal stop ────────────────────────────────────────────────────────────


def test_a_normal_stop_closes_the_shell_sessions_between_the_two_service_stops() -> None:
    # meiva:note
    # The pty-sessions unit outlives the services phase: it holds the shells it closes, and
    # goes down only once nothing is left in it. Keeping the browser keeps the detached Chrome
    # process while stopping the root keeper unit so meiva-root itself stops.
    assert steps(choices()) == (
        ClearServing(),
        services(PTY),
        CloseShellSessions(direct_db=False, force=False),
        services(phase="pty-sessions"),
    )


def test_keeping_the_shell_sessions_keeps_their_service_and_closes_nothing() -> None:
    assert steps(choices(keep_shell_sessions=True)) == (
        ClearServing(),
        services(PTY),
    )


def test_a_stop_that_stops_the_browser_records_its_tabs_before_anything_signals_it() -> None:
    plan = steps(choices(keep_browser=False))
    assert plan[0] == RecordBrowserSession()
    assert plan.index(RecordBrowserSession()) < plan.index(services(PTY))
    assert StopBrowser() in plan
    assert plan.index(services(PTY)) < plan.index(StopBrowser())


def test_a_kept_browser_is_neither_recorded_nor_stopped() -> None:
    plan = steps(choices(keep_browser=True))
    assert RecordBrowserSession() not in plan and StopBrowser() not in plan


def test_announcing_follows_the_revocation_of_serving_and_carries_the_window() -> None:
    window = StopWindow(for_seconds=600, reason="disk swap")
    plan = steps(choices(announce=True), window=window)
    assert plan[:2] == (ClearServing(), Announce(window))


def test_extras_are_torn_down_exactly_when_asked_and_only_with_nothing_retained() -> None:
    assert StopExtras(frozenset()) not in steps(choices())
    plan = steps(choices(keep_browser=False, teardown_extras=True))
    assert plan[-1] == StopExtras(frozenset())


@pytest.mark.parametrize(
    ("keep_infra", "stops"), [(True, False), (False, True)], ids=["kept", "stopped"]
)
def test_a_gateway_stops_its_data_plane_exactly_when_the_operator_does_not_keep_it(
    keep_infra: bool, stops: bool
) -> None:
    plan = plan_stop(choices(keep_infra=keep_infra), GATEWAY, WINDOW)
    assert (StopDataPlane(force=False) in plan.steps) is stops
    assert plan.scope.data_plane is (DataPlane.STOPPED if stops else DataPlane.KEPT)


def test_the_data_plane_is_stopped_after_everything_that_needs_it() -> None:
    plan = steps(choices(keep_infra=False, keep_browser=False, teardown_extras=True), GATEWAY)
    assert plan[-2:] == (StopExtras(frozenset()), StopDataPlane(force=False))
    assert CloseShellSessions(direct_db=True, force=False) in plan


def test_a_host_without_the_gateway_capability_has_no_data_plane_step_and_dials_no_pooler() -> None:
    plan = plan_stop(choices(), RUNNER, WINDOW)
    assert plan.scope.data_plane is DataPlane.NONE
    assert CloseShellSessions(direct_db=False, force=False) in plan.steps
    assert not any(isinstance(step, StopDataPlane | SkipDataPlane) for step in plan.steps)


def test_a_kept_service_stays_up_through_both_service_stops() -> None:
    assert steps(choices(preserve=frozenset({"frontend"}))) == (
        ClearServing(),
        services("frontend", PTY),
        CloseShellSessions(direct_db=False, force=False),
        services("frontend", phase="pty-sessions"),
    )


def test_a_stop_message_travels_to_stopped_agent_host_and_smooth_shell_closure() -> None:
    reason = "Zhiyuan rolling out #9999"
    assert steps(choices(message=reason)) == (
        ClearServing(),
        services(PTY, message=reason),
        CloseShellSessions(direct_db=False, force=False, message=reason),
        services(phase="pty-sessions"),
    )
    assert steps(choices(preserve=frozenset({"agent-host"}), message=reason)) == (
        ClearServing(),
        services("agent-host", PTY),
        CloseShellSessions(direct_db=False, force=False, message=reason),
        services("agent-host", phase="pty-sessions"),
    )
    assert steps(choices(force=True, message=reason)) == (
        ClearServing(),
        services(PTY, force=True),
        CloseShellSessions(direct_db=False, force=True),
        services(force=True, phase="pty-sessions"),
        SkipDataPlane(DataPlane.NONE),
    )


def test_a_proven_unstarted_attempt_is_closed_without_draining_announcing_or_revoking() -> None:
    host = replace(GATEWAY, unstarted=True)
    plan = steps(choices(keep_infra=False, keep_browser=False, teardown_extras=True), host)
    assert plan == (
        RecordBrowserSession(),
        services(),
        StopBrowser(),
        StopExtras(frozenset()),
        StopDataPlane(force=False),
    )
    assert ClearServing() not in plan and not any(isinstance(s, Announce) for s in plan)


def test_an_unstarted_attempt_keeping_its_data_plane_stops_only_the_services() -> None:
    assert steps(choices(keep_infra=True), replace(GATEWAY, unstarted=True)) == (services(),)


def test_the_restart_stop_leg_replaces_services_and_closes_shells_but_keeps_browser_and_data_plane() -> (
    None
):
    leg = restart_stop_request(restart_request(), roles=GATEWAY.roles).choices
    plan = plan_stop(leg, GATEWAY, WINDOW)
    assert plan.steps == (
        ClearServing(),
        services("browser", PTY),
        CloseShellSessions(direct_db=True, force=False),
        services("browser", phase="pty-sessions"),
    )
    assert plan.scope.data_plane is DataPlane.KEPT


def test_a_restart_stop_leg_on_a_host_without_a_gateway_has_no_data_plane_choice() -> None:
    leg = restart_stop_request(
        restart_request(keep_browser=False, keep_shell_sessions=True, restart_extras=True),
        roles=RUNNER.roles,
    ).choices
    assert leg.keep_infra is None
    # meiva:note
    # Extras cannot be retired while a unit is retained: a restart that keeps its shell
    # sessions cannot also retire the extras.
    with pytest.raises(ValueError, match="--keep-shell-sessions"):
        plan_stop(leg, RUNNER, WINDOW)


# meiva:note
# ── a force stop ─────────────────────────────────────────────────────────────


def test_a_force_stop_interrupts_the_services_then_closes_the_shells_through_the_service() -> None:
    assert steps(choices(force=True)) == (
        ClearServing(),
        services(PTY, force=True),
        CloseShellSessions(direct_db=False, force=True),
        services(force=True, phase="pty-sessions"),
        SkipDataPlane(DataPlane.NONE),
    )


def test_a_force_stop_that_stops_the_browser_records_it_and_reaps_the_chrome_that_escaped() -> None:
    plan = steps(choices(force=True, keep_browser=False, announce=True))
    assert plan[:4] == (
        ClearServing(),
        Announce(WINDOW),
        RecordBrowserSession(),
        services(PTY, force=True),
    )
    assert plan[4] == ReapChrome()


def test_a_force_stop_keeping_the_shell_sessions_closes_none() -> None:
    plan = steps(choices(force=True, keep_shell_sessions=True))
    assert plan == (
        ClearServing(),
        services(PTY, force=True),
        SkipDataPlane(DataPlane.NONE),
    )


@pytest.mark.parametrize("teardown_extras", [True, False])
def test_a_force_stop_tears_down_extras_exactly_when_asked(teardown_extras: bool) -> None:
    plan = steps(choices(force=True, keep_browser=False, teardown_extras=teardown_extras))
    assert (StopExtras(frozenset()) in plan) is teardown_extras


@pytest.mark.parametrize(
    ("keep_infra", "last"),
    [(True, SkipDataPlane(DataPlane.KEPT)), (False, StopDataPlane(force=True))],
    ids=["kept", "stopped"],
)
def test_a_force_stop_narrates_a_kept_data_plane_and_stops_one_that_is_not(
    keep_infra: bool, last: SkipDataPlane | StopDataPlane
) -> None:
    assert steps(choices(force=True, keep_infra=keep_infra), GATEWAY)[-1] == last


def test_a_force_stop_lists_the_service_tree_against_the_units_it_keeps() -> None:
    scope = plan_stop(choices(force=True, keep_shell_sessions=True), RUNNER, WINDOW).scope
    assert scope.force and scope.tree_preserve == frozenset({PTY})
    assert plan_stop(choices(), RUNNER, WINDOW).scope.tree_preserve is None


# meiva:note
# ── the phases a normal stop times ───────────────────────────────────────────


def test_every_step_that_does_work_has_a_phase_and_the_rest_have_none() -> None:
    plan = steps(
        choices(keep_infra=True, keep_browser=False, teardown_extras=True, announce=True), GATEWAY
    )
    assert [phase_label(step) for step in plan] == [
        "browser-session",
        None,
        None,
        "services",
        "browser",
        "shell-sessions",
        "pty-sessions",
        "extras",
    ]


# meiva:note
# ── refusals: every contradiction is a ValueError before a unit is signalled ──


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("keep_infra", [True, False])
def test_a_host_without_the_gateway_capability_refuses_a_data_plane_choice(
    force: bool, keep_infra: bool
) -> None:
    with pytest.raises(ValueError, match="no local data plane"):
        steps(choices(keep_infra=keep_infra, force=force), RUNNER)


@pytest.mark.parametrize("force", [False, True])
def test_a_gateway_requires_a_data_plane_choice(force: bool) -> None:
    with pytest.raises(ValueError, match="choose --keep-infra or --stop-infra"):
        steps(choices(keep_infra=None, force=force), GATEWAY)


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("dedicated", [PTY, "browser"])
def test_keep_service_cannot_name_a_service_with_its_own_choice(
    force: bool, dedicated: str
) -> None:
    with pytest.raises(ValueError, match="--keep-shell-sessions"):
        steps(choices(preserve=frozenset({dedicated}), force=force))


@pytest.mark.parametrize("force", [False, True])
def test_an_unknown_kept_service_is_refused(force: bool) -> None:
    with pytest.raises(ValueError, match=r"unknown preserved service\(s\): \['nonesuch'\]"):
        steps(choices(preserve=frozenset({"nonesuch"}), force=force))


@pytest.mark.parametrize("force", [False, True])
def test_a_kept_service_that_needs_the_database_cannot_outlive_the_data_plane(
    force: bool,
) -> None:
    with pytest.raises(ValueError, match=r"preserved services require --keep-infra: \['gateway'\]"):
        steps(choices(preserve=frozenset({"gateway"}), keep_infra=False, force=force), GATEWAY)
    # meiva:note
    # With the data plane kept, the same service may stay.
    steps(choices(preserve=frozenset({"gateway"}), keep_infra=True, force=force), GATEWAY)


@pytest.mark.parametrize(
    ("keep_shell_sessions", "keep_browser", "preserve", "named"),
    [
        (True, False, frozenset[str](), "--keep-shell-sessions"),
        (False, True, frozenset[str](), "--keep-browser"),
        (False, False, frozenset({"frontend"}), "--keep-service"),
    ],
)
def test_each_keep_choice_conflicts_with_retiring_extras(
    keep_shell_sessions: bool, keep_browser: bool, preserve: frozenset[str], named: str
) -> None:
    conflicting = choices(
        teardown_extras=True,
        keep_shell_sessions=keep_shell_sessions,
        keep_browser=keep_browser,
        preserve=preserve,
    )
    with pytest.raises(ValueError) as refusal:
        require_consistent(conflicting, pty_unit=PTY)
    assert named in str(refusal.value)
    # meiva:note
    # Both ways out are named: the operator never has to guess.
    assert "Drop them, or use --keep-extras" in str(refusal.value)
    with pytest.raises(ValueError, match="Drop them, or use --keep-extras"):
        steps(replace(conflicting, force=True))


@pytest.mark.parametrize(
    ("teardown_extras", "keep_shell_sessions", "keep_browser", "preserve"),
    [(True, False, False, frozenset[str]()), (False, True, True, frozenset({"frontend"}))],
)
def test_a_compatible_extras_choice_is_admitted(
    teardown_extras: bool, keep_shell_sessions: bool, keep_browser: bool, preserve: frozenset[str]
) -> None:
    require_consistent(
        choices(
            teardown_extras=teardown_extras,
            keep_shell_sessions=keep_shell_sessions,
            keep_browser=keep_browser,
            preserve=preserve,
        ),
        pty_unit=PTY,
    )


# meiva:note
# Force is the stop that must work when the settings the roster's ports derive from
# cannot be loaded: the shell reads no roster for it, and the plan has nothing to check.
def test_a_force_stop_needs_no_roster_and_so_checks_the_kept_services_against_none() -> None:
    offline = replace(RUNNER, roster=None)
    plan = plan_stop(choices(force=True, preserve=frozenset({"nonesuch"})), offline, WINDOW)
    assert services("nonesuch", PTY, force=True) in plan.steps


def test_a_stop_that_is_not_forced_cannot_be_planned_without_the_roster() -> None:
    with pytest.raises(ValueError, match="planned against the service roster"):
        plan_stop(choices(), replace(RUNNER, roster=None), WINDOW)
