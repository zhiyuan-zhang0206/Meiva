# meiva:note
# What a stop does, in what order, and what it keeps: a plan made from values.
#
# There are three shapes, chosen by the operator's `force` and by the host's evidence:
#
# - a normal stop, whose services phase keeps the pty-sessions unit until its shell sessions
#   are closed, and which never escalates a service or data-plane stop;
# - the close of a proven pre-application attempt (`StopHost.unstarted`), which has nothing to
#   drain, announce or revoke;
# - an explicit force stop, which interrupts and closes the shell sessions without grace.
#
# Every illegal combination of choices is a `ValueError` raised here, before the shell has
# signalled a single unit.

from __future__ import annotations

from base.deploy.lifecycle import service_selection
from cli.commands.lifecycle.stop.choices import (
    BROWSER_SESSION,
    StopChoices,
    StopHost,
    StopWindow,
)
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
    StopPlan,
    StopScope,
    StopServices,
    StopStep,
)

__all__ = ["plan_stop", "require_consistent"]


# meiva:note
# Refuse the combinations of choices that contradict one another, from the choices alone.
#
# The shell runs this before it reads a single fact of the host, so a refusal touches
# nothing. `pty_unit` is the roster unit that has its own `--keep-shell-sessions` choice.
def require_consistent(choices: StopChoices, *, pty_unit: str) -> None:
    # meiva:note
    # The rule and its words live in `service_selection.extras_keep_refusal`, which the fleet's
    # parser also uses, so an operator cannot be told two different things about one stop.
    refusal = service_selection.extras_keep_refusal(
        stop_extras=choices.teardown_extras,
        keep_shell_sessions=choices.keep_shell_sessions,
        keep_browser=choices.keep_browser,
        keep_service=bool(choices.preserve_sessions),
    )
    if refusal:
        raise ValueError(refusal)
    # meiva:note
    # Shell sessions and the browser have their own choices, never `--keep-service`.
    dedicated = choices.preserve_sessions & {pty_unit, BROWSER_SESSION}
    if dedicated:
        raise ValueError(
            f"--keep-service {sorted(dedicated)}: choose --keep-shell-sessions/"
            "--close-shell-sessions and --keep-browser/--stop-browser instead"
        )


# meiva:note
# The data-plane choice must match this host: a machine fact only refuses.
#
# A host with the gateway capability owns a local data plane and requires an explicit
# keep-or-stop answer; any other host has none and accepts no answer.
def _data_plane(roles: frozenset[str], *, keep_infra: bool | None) -> DataPlane:
    if "gateway" in roles and keep_infra is None:
        raise ValueError("this host serves the gateway: choose --keep-infra or --stop-infra")
    if "gateway" not in roles:
        if keep_infra is not None:
            raise ValueError(
                "this host has no local data plane: --keep-infra/--stop-infra do not apply"
            )
        return DataPlane.NONE
    return DataPlane.KEPT if keep_infra else DataPlane.STOPPED


# meiva:note
# The root-supervised units this stop leaves running, refusing any it cannot honour.
#
# `meiva stop --keep-browser` keeps the detached Chrome process (by omitting
# RecordBrowserSession, StopBrowser and ReapChrome) while stopping the `meiva-browser`
# keeper unit with the rest of the root service tree so `meiva-root` itself stops and can
# be replaced across updates. An in-place `meiva restart` sets `keep_browser_unit` so the
# live root keeps the `browser` unit across its stop and start legs.
def _preserved(choices: StopChoices, host: StopHost, plane: DataPlane) -> frozenset[str]:
    preserved = choices.preserve_sessions
    if choices.keep_shell_sessions:
        preserved |= {host.pty_unit}
    if choices.keep_browser_unit:
        preserved |= {BROWSER_SESSION}
    roster = host.roster
    if roster is None:
        if not choices.force:
            raise ValueError("a stop that is not forced is planned against the service roster")
        return preserved
    unknown = preserved - roster.known
    if unknown:
        raise ValueError(f"unknown preserved service(s): {sorted(unknown)}")
    if plane is DataPlane.STOPPED:
        dependent = sorted(preserved & roster.requires_db)
        if dependent:
            raise ValueError(f"preserved services require --keep-infra: {dependent}")
    return preserved


# meiva:note
# The plan for this stop; raises `ValueError` for a combination of choices it cannot honour.
def plan_stop(choices: StopChoices, host: StopHost, window: StopWindow) -> StopPlan:
    require_consistent(choices, pty_unit=host.pty_unit)
    plane = _data_plane(host.roles, keep_infra=choices.keep_infra)
    preserved = _preserved(choices, host, plane)
    if choices.force:
        scope = StopScope(
            force=True,
            shells_kept=choices.keep_shell_sessions,
            browser_kept=choices.keep_browser,
            data_plane=plane,
            tree_preserve=preserved,
        )
        return StopPlan(_force_steps(choices, host, window, preserved, plane), scope)
    scope = StopScope(
        force=False,
        shells_kept=choices.keep_shell_sessions,
        browser_kept=choices.keep_browser,
        data_plane=plane,
        tree_preserve=None,
    )
    if host.unstarted:
        return StopPlan(_initialization_steps(choices, plane), scope)
    return StopPlan(_selected_steps(choices, host, window, preserved, plane), scope)


def _record_browser(choices: StopChoices) -> tuple[StopStep, ...]:
    # meiva:note
    # The record is the first thing a stop does with the browser, ahead of the phase that signals
    # `meiva-browser`: waiting for the stop's own `browser` phase would find Chrome already gone.
    return () if choices.keep_browser else (RecordBrowserSession(),)


def _selected_steps(
    choices: StopChoices,
    host: StopHost,
    window: StopWindow,
    preserved: frozenset[str],
    plane: DataPlane,
) -> tuple[StopStep, ...]:
    # meiva:note
    # Serving is revoked first, so nothing that reads the gate sees a stopping unit as ready. The
    # services phase is where the agents settle: root stops agent-host first and waits for it,
    # and its own SIGTERM exit gives every in-flight turn a restart the next host resumes. The
    # pty-sessions unit outlives that phase: it closes the shell sessions, then it goes too.
    steps: list[StopStep] = [*_record_browser(choices), ClearServing()]
    if choices.announce:
        steps.append(Announce(window))
    host_message = None if "agent-host" in preserved else choices.message
    steps.append(
        StopServices(
            preserved | {host.pty_unit}, force=False, phase="services", message=host_message
        )
    )
    if not choices.keep_browser:
        steps.append(StopBrowser())
    if not choices.keep_shell_sessions:
        steps.append(
            CloseShellSessions(
                direct_db="gateway" in host.roles, force=False, message=choices.message
            )
        )
        steps.append(StopServices(preserved, force=False, phase="pty-sessions"))
    if choices.teardown_extras:
        steps.append(StopExtras(preserved))
    if plane is DataPlane.STOPPED:
        steps.append(StopDataPlane(force=False))
    return tuple(steps)


def _initialization_steps(choices: StopChoices, plane: DataPlane) -> tuple[StopStep, ...]:
    # meiva:note
    # A proven pre-application attempt: nothing was served, no shell session exists, so the
    # native owners close what the attempt left and the stop announces nothing.
    steps: list[StopStep] = [
        *_record_browser(choices),
        StopServices(frozenset(), force=False, phase="services"),
    ]
    if not choices.keep_browser:
        steps.append(StopBrowser())
    if choices.teardown_extras:
        steps.append(StopExtras(frozenset()))
    if plane is DataPlane.STOPPED:
        steps.append(StopDataPlane(force=False))
    return tuple(steps)


def _force_steps(
    choices: StopChoices,
    host: StopHost,
    window: StopWindow,
    preserved: frozenset[str],
    plane: DataPlane,
) -> tuple[StopStep, ...]:
    # meiva:note
    # Explicit force interrupts the host process. Agent metadata and checkpoints stay untouched;
    # the next host uses its existing owner recovery. It leaves no restart behind for the
    # interrupted turns and stays usable offline. The pty-sessions unit outlives the first stop:
    # it closes the shell sessions, then it goes too.
    steps: list[StopStep] = [ClearServing()]
    if choices.announce:
        steps.append(Announce(window))
    steps.extend(_record_browser(choices))
    steps.append(StopServices(preserved | {host.pty_unit}, force=True, phase="services"))
    if not choices.keep_browser:
        # meiva:note
        # A Chrome that left the session tree on a `SingletonLock` handoff holds the cluster's
        # CDP port against the next launch. Placed after the services stop on purpose: the
        # watchdog is dead by now, so nothing relaunches Chrome onto the port just cleared.
        steps.append(ReapChrome())
    if not choices.keep_shell_sessions:
        steps.append(CloseShellSessions(direct_db="gateway" in host.roles, force=True))
        steps.append(StopServices(preserved, force=True, phase="pty-sessions"))
    if choices.teardown_extras:
        steps.append(StopExtras(preserved))
    steps.append(StopDataPlane(force=True) if plane is DataPlane.STOPPED else SkipDataPlane(plane))
    return tuple(steps)
