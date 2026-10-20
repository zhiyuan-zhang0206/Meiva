"""Shared native stop boundary for operator commands and updates."""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from collections.abc import Callable

import psutil

from base.agents.context.clients import DatabaseFactory
from base.cluster import session_name
from base.cluster.machine import MachineIdentity, MachineRoles
from base.deploy.lifecycle import start_serving
from base.deploy.lifecycle.status_journal import begin, finish, phase, status_path
from base.host.permissions_helper.client import HelperFactory
from base.native_process.ownership import OwnedProcess, capture_tree, retain_processes
from base.sessions.pty.paths import SERVICE_UNIT
from base.telemetry import EventPipeline
from cli.commands.data_plane.stop_waits import deadline_after, remaining, wait_for_exit
from cli.commands.lifecycle.service_stop import close_terminals, stop_data_plane
from services.supervision.roster import build_services


def stop_browser(deadline: float) -> None:
    from services.desktop.browser.orphan import find_cluster_chrome

    trees: set[OwnedProcess] = set()
    for pid in find_cluster_chrome():
        try:
            identity = OwnedProcess.capture(psutil.Process(pid))
            retain_processes(trees, capture_tree(identity))
            if identity.live():
                identity.send_signal(signal.SIGTERM)
        except psutil.NoSuchProcess:
            continue
    wait_for_exit(trees, deadline)
    if find_cluster_chrome():
        raise RuntimeError("this unit's browser appeared during stop")


def stop_extras(deadline: float) -> None:
    from cli.commands.lifecycle._stop_extras import stop_permissions_helper

    stop_permissions_helper(timeout_s=remaining(deadline))


def report_incomplete(
    exc: BaseException,
    phases: list[tuple[str, float]],
    *,
    owns_journal: bool,
) -> None:
    """Print the stop's real failure with its per-phase budget accounting and
    close the lifecycle journal when this stop owns it.

    The journal record carries the structured evidence too: the failing stage
    and the surviving-process inventory a `StopIncompleteError` collected, so the
    diagnosis survives the process that printed it (issue #2162). A failed stop
    is reported as it is: nothing is restarted behind the operator's back.
    """
    timing = "; ".join(f"{label} {elapsed:.1f}s" for label, elapsed in phases)
    print(
        f"Stop incomplete; services and the data plane were not force-killed: {exc}. "
        f"phases: {timing or 'before the first phase'}. "
        "Retry the command, or use meiva start (with this unit's explicit service selection) to resume. "
        f"Status journal: {status_path()}.",
        file=sys.stderr,
    )
    if owns_journal:
        extra: dict[str, object] = {"phases": timing}
        stage = getattr(exc, "stage", None)
        survivors = getattr(exc, "survivors", None)
        if stage:
            extra["stage"] = stage
        if survivors:
            extra["survivors"] = survivors
        finish(1, error=f"{type(exc).__name__}: {exc}", extra=extra)


def _timed_phase(phases: list[tuple[str, float]], label: str, step: Callable[[], object]) -> None:
    """Run one stop phase, recording its wall time even when it raises.

    The phase name is printed BEFORE the step runs so a caller whose outer
    budget cuts the command off mid-phase still saw which stage it entered —
    and the lifecycle journal carries the same timeline durably.
    """
    started = time.monotonic()
    print(f"  · stop phase: {label}", flush=True)
    with phase(label):
        try:
            step()
        except BaseException:
            phases.append((label, time.monotonic() - started))
            raise
        phases.append((label, time.monotonic() - started))


def require_infra_choice(roles: MachineRoles, *, keep_infra: bool | None) -> None:
    # meiva:note
    # The data-plane choice must match this host: a machine fact only refuses.
    #
    # A host with the gateway capability owns a local data plane and requires an
    # explicit keep-or-stop answer; any other host has none and accepts no answer.
    if "gateway" in roles and keep_infra is None:
        raise ValueError("this host serves the gateway: choose --keep-infra or --stop-infra")
    if "gateway" not in roles and keep_infra is not None:
        raise ValueError(
            "this host has no local data plane: --keep-infra/--stop-infra do not apply"
        )


def require_dedicated_choices(preserve_sessions: frozenset[str]) -> None:
    # meiva:note
    # Shell sessions and the browser have their own choices, never `--keep-service`.
    dedicated = preserve_sessions & {SERVICE_UNIT, "browser"}
    if dedicated:
        raise ValueError(
            f"--keep-service {sorted(dedicated)}: choose --keep-shell-sessions/"
            "--close-shell-sessions and --keep-browser/--stop-browser instead"
        )


def stop_plan(
    *,
    preserve_sessions: frozenset[str],
    keep_browser: bool,
    keep_shell_sessions: bool,
    keep_infra: bool | None,
    machine: MachineIdentity,
) -> tuple[MachineRoles, frozenset[str], frozenset[str]]:
    # meiva:note
    # Resolve this stop's service selection from the roster and roles.
    #
    # Refuses a data-plane choice that does not match this host, unknown preserved
    # sessions and preserved services that need the data plane while it is being
    # stopped.
    roles = machine.role()
    require_infra_choice(roles, keep_infra=keep_infra)
    require_dedicated_choices(preserve_sessions)
    preserved = (
        preserve_sessions
        | (frozenset({"browser"}) if keep_browser else frozenset[str]())
        | (frozenset({SERVICE_UNIT}) if keep_shell_sessions else frozenset[str]())
    )
    specs = build_services()
    known = {spec.session for spec in specs}
    unknown = preserved - known
    if unknown:
        raise ValueError(f"unknown preserved service(s): {sorted(unknown)}")
    if "gateway" in roles and not keep_infra:
        dependent = sorted(
            spec.session for spec in specs if spec.session in preserved and spec.requires_db
        )
        if dependent:
            raise ValueError(f"preserved services require --keep-infra: {dependent}")
    selected = frozenset(
        session_name(spec.session) for spec in specs if spec.session not in preserved
    )
    return roles, selected, preserved


def services_phase_action(
    *, preserved: frozenset[str], deadline: float, helper: HelperFactory | None
) -> Callable[[], object]:
    """Stop services through their root owner, preserving explicitly retained units."""
    import cli.commands.lifecycle.root_driver as _root_driver_commands

    return lambda: _root_driver_commands.stop_root_service_tree(
        preserve=preserved, timeout_s=remaining(deadline), helper=helper
    )


def require_unstarted_initialization() -> bool:
    """Positive first-start evidence that no application could have admitted work."""
    from base.paths import meiva_home, root_manifests_path
    from cli.commands.lifecycle.root_driver import require_root_absent
    from cli.commands.lifecycle.service_stop import require_no_terminals
    from cli.start_identity import read_intent

    intent = read_intent(meiva_home())
    if intent is None or intent["phase"] != "configured":
        return False
    if "gateway" not in intent["roles"]:
        return False  # A joined runner may refer to already-existing external work.
    if start_serving.state_path().exists() or root_manifests_path(meiva_home()).exists():
        raise RuntimeError("initialization journal conflicts with application launch evidence")
    require_root_absent()
    require_no_terminals()
    return True


def _stop_initialization(
    phases: list[tuple[str, float]],
    deadline: float,
    *,
    keep_infra: bool | None,
    keep_browser: bool,
    teardown_extras: bool,
    notes: list[str],
    clients: list[str],
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    producer: Callable[[], EventPipeline],
    helper: HelperFactory | None,
) -> None:
    """Close a proven pre-application attempt through the existing native owners."""
    _timed_phase(
        phases,
        "services",
        services_phase_action(preserved=frozenset(), deadline=deadline, helper=helper),
    )
    if not keep_browser:
        _timed_phase(phases, "browser", lambda: stop_browser(deadline))
    if teardown_extras:
        _timed_phase(phases, "extras", lambda: stop_extras(deadline))
    if keep_infra is False:
        _timed_phase(
            phases,
            "data-plane",
            lambda: stop_data_plane(
                remaining(deadline),
                save=True,
                notes=notes,
                clients=clients,
                retained_children=retained_children,
                producer=producer,
            ),
        )


def refuse_hosted_stop() -> None:
    # meiva:note
    # A stop run from inside the work it drains strands itself mid-drain.
    from base.host.proc import hosting_exec_domain
    from base.sessions.hosting import hosting_supervised_session

    # meiva:note
    # An exec-domain leg is SIGKILLed with the call's process group as the tool
    # call returns, mid-drain (the 2026-09-12 stranding shape). A stop closes this
    # unit's persistent terminals too, so no terminal can host it either.
    if hosting_exec_domain() is not None:
        raise RuntimeError(
            "stop cannot run inside execute_code: the call's teardown SIGKILLs its "
            "process group as the call returns, stranding the stop mid-drain; a stop "
            "closes this unit's persistent terminals too — run it from a shell no meiva "
            "session hosts (e.g. a plain login shell)"
        )
    if hosting_supervised_session() is not None:
        raise RuntimeError("stop must run outside the work it drains; use a login shell")


# meiva:note
# Stop the selected resources; each phase is timed into `phases`.
#
# Serving is revoked first, so nothing that reads the gate sees a stopping unit as ready.
# The services phase is where the agents settle: root stops agent-host first and waits for
# it, and its own SIGTERM exit gives every in-flight turn a restart the next host resumes
# (`services/agent_runner/agent_host/lifecycle/graceful_exit.py`).
def _stop_selected(
    phases: list[tuple[str, float]],
    deadline: float,
    *,
    roles: MachineRoles,
    preserved: frozenset[str],
    keep_infra: bool | None,
    keep_browser: bool,
    keep_shell_sessions: bool,
    announce: bool,
    teardown_extras: bool,
    notes: list[str],
    clients: list[str],
    maintenance_for_seconds: int | None = None,
    maintenance_reason: str | None = None,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    database_factory: DatabaseFactory,
    producer: Callable[[], EventPipeline],
    machine: MachineIdentity,
    helper: HelperFactory | None,
) -> None:
    start_serving.clear_serving()
    if announce:
        announce_stopping(
            machine=machine,
            maintenance_for_seconds=maintenance_for_seconds,
            maintenance_reason=maintenance_reason,
        )
    # meiva:note
    # The pty-sessions service outlives the services phase: it closes the terminals
    # below. `--keep-shell-sessions` keeps the service and the shells it holds.
    keep_terminals = keep_shell_sessions
    _timed_phase(
        phases,
        "services",
        services_phase_action(
            preserved=preserved | {SERVICE_UNIT}, deadline=deadline, helper=helper
        ),
    )
    if not keep_browser and "browser" not in preserved:
        _timed_phase(phases, "browser", lambda: stop_browser(deadline))
    if not keep_terminals:
        _timed_phase(
            phases,
            "terminals",
            lambda: close_terminals(
                deadline,
                direct_db="gateway" in roles,
                database_factory=database_factory,
                local_machine=machine,
            ),
        )
        # meiva:note
        # Nothing is left in the service: stop it, and the root with it when nothing else is kept.
        _timed_phase(
            phases,
            "pty-sessions",
            services_phase_action(preserved=preserved, deadline=deadline, helper=helper),
        )
    if teardown_extras:
        _timed_phase(phases, "extras", lambda: stop_extras(deadline))
    if "gateway" in roles and keep_infra is False:
        _timed_phase(
            phases,
            "data-plane",
            lambda: stop_data_plane(
                remaining(deadline),
                save=True,
                notes=notes,
                clients=clients,
                retained_children=retained_children,
                producer=producer,
            ),
        )


def stop(
    *,
    require_confirmation: bool,
    keep_infra: bool | None,
    preserve_sessions: frozenset[str],
    keep_browser: bool,
    keep_shell_sessions: bool,
    announce: bool,
    teardown_extras: bool,
    timeout: float,
    maintenance_for_seconds: int | None = None,
    maintenance_reason: str | None = None,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    database_factory: DatabaseFactory,
    producer: Callable[[], EventPipeline],
    machine: MachineIdentity,
    helper: HelperFactory | None,
) -> int:
    # meiva:note
    # Stop the selected resources; agent-host settles the in-flight turns on its own SIGTERM.
    #
    # Services and the data plane are never forced. Closing terminals (skipped
    # only with `keep_shell_sessions`) SIGKILLs what outlives its bounded grace
    # (`service_stop.close_terminals`).
    refuse_hosted_stop()
    roles, _selected, preserved = stop_plan(
        preserve_sessions=preserve_sessions,
        keep_browser=keep_browser,
        keep_shell_sessions=keep_shell_sessions,
        keep_infra=keep_infra,
        machine=machine,
    )
    data_plane = {None: "none on this host", True: "retained", False: "stopped"}[keep_infra]
    print(
        "[meiva stop] local services; "
        f"terminals={'retained' if keep_shell_sessions else 'closed'}; "
        f"data plane={data_plane}"
    )
    if not confirm_stop(require_confirmation=require_confirmation):
        return 0
    deadline = deadline_after(timeout)
    owns_journal = begin("stop", deadline=deadline)
    # meiva:note
    # Per-phase accounting: a failed stop names each phase and how much of the
    # shared budget it consumed, so the operator sees WHERE the budget went
    # (services vs terminals vs data plane) — never a bare timeout (#2045).
    phases: list[tuple[str, float]] = []
    notes: list[str] = []  # what the report must say: a Postgres shutdown that was escalated
    clients: list[str] = []  # the pooler's clients still connected at its stop (reported only)

    try:
        if require_unstarted_initialization():
            _stop_initialization(
                phases,
                deadline,
                keep_infra=keep_infra,
                keep_browser=keep_browser,
                teardown_extras=teardown_extras,
                notes=notes,
                clients=clients,
                retained_children=retained_children,
                producer=producer,
                helper=helper,
            )
            return finish_stop(owns_journal=owns_journal, notes=notes, clients=clients)
        _stop_selected(
            phases,
            deadline,
            roles=roles,
            preserved=preserved,
            keep_infra=keep_infra,
            keep_browser=keep_browser,
            keep_shell_sessions=keep_shell_sessions,
            announce=announce,
            teardown_extras=teardown_extras,
            notes=notes,
            clients=clients,
            maintenance_for_seconds=maintenance_for_seconds,
            maintenance_reason=maintenance_reason,
            retained_children=retained_children,
            database_factory=database_factory,
            producer=producer,
            machine=machine,
            helper=helper,
        )
    except (RuntimeError, TimeoutError, OSError, subprocess.TimeoutExpired) as exc:
        report_incomplete(exc, phases, owns_journal=owns_journal)
        return 1
    return finish_stop(owns_journal=owns_journal, notes=notes, clients=clients)


def finish_stop(*, owns_journal: bool, notes: list[str], clients: list[str] | None = None) -> int:
    """The stop is done; `notes` (an escalated Postgres shutdown) and `clients` (the pooler's
    clients still connected at its stop) go into the journal."""
    for note in notes:
        print(f"Stop completed with an escalation: {note}", file=sys.stderr)
    if owns_journal:
        extra: dict[str, object] = {}
        if notes:
            extra["escalations"] = notes
        if clients:
            extra["pooler_clients"] = clients
        finish(0, extra=extra or None)
    return 0


def confirm_stop(*, require_confirmation: bool) -> bool:
    """The stdin "y/N" gate. Returns True when the stop may proceed."""
    if not require_confirmation:
        return True
    try:
        answer = input("confirm? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer != "y":
        print("aborted")
        return False
    return True


def announce_stopping(
    *,
    machine: MachineIdentity,
    maintenance_for_seconds: int | None = None,
    maintenance_reason: str | None = None,
) -> None:
    """Best-effort POST `/api/cluster/stopping` before local teardown.

    Stamps the stopping unit's `stopped_at` and recomputes the composed
    `machines` row so the cluster view distinguishes an intentional `meiva stop`
    from a crash. `home` identifies THIS unit (a co-located peer keeps its
    caps). Best-effort by design: if the gateway is unreachable we are stopping
    anyway, so we log and proceed rather than block the teardown.
    """
    from base.cluster.machine import gateway_api_base, gateway_auth_headers
    from base.cluster.machine.ambient import gateway_access_from_settings
    from base.host.net.http_dial import post as dial_post
    from base.paths import meiva_home

    access = gateway_access_from_settings()  # ambient: C
    try:
        name = machine.name()
        home = str(meiva_home())
        url = f"{gateway_api_base(access.gateway_url)}/api/cluster/stopping"
        params = {"machine": name, "home": home}
        if maintenance_for_seconds is not None:
            params["maintenance_for_seconds"] = str(maintenance_for_seconds)
        if maintenance_reason is not None:
            params["maintenance_reason"] = maintenance_reason
        resp = dial_post(
            url,
            params=params,
            timeout=5.0,
            headers=gateway_auth_headers(access),
        )
        resp.raise_for_status()
        print(f"[meiva stop] announced intentional shutdown of {name!r} ({home}) to the gateway")
    except Exception as e:
        print(f"[meiva stop] could not announce shutdown (proceeding anyway): {e}")
