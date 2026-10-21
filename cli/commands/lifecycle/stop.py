# meiva:note
# Local stop and restart commands over the shared native stop boundary.
#
# Normal paths retain checkpoints and never escalate a service or data-plane
# stop on timeout; a normal stop's shell-session closure SIGKILLs what outlives its
# bounded grace. Explicit force uses the separate legacy resource teardown.
# Every keep-or-stop choice (shell sessions, browser, extras, data plane, other
# services), the interruption mode and the budget are the caller's explicit
# arguments; nothing here defaults or infers one.

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from base.agents.context.clients import DatabaseFactory
from base.cluster import session_name
from base.cluster.machine import MachineIdentity
from base.host.permissions_helper.client import HelperFactory
from base.sessions.hosting import hosting_supervised_session
from base.sessions.pty.paths import SERVICE_UNIT
from base.telemetry import EventPipeline
from cli.commands._repo import repo_root
from cli.commands.data_plane.stop_waits import deadline_after, remaining
from cli.commands.lifecycle import _temporary_stop, root_driver, root_readiness
from cli.commands.lifecycle._exclusive_resources import exclusive_resources
from cli.commands.lifecycle.service_stop import force_close_shell_sessions
from cli.database import OperatorDatabaseFactory
from cli.database.lifecycle_audit import (
    operator_lifecycle_audit,
    record_lifecycle_request,
)
from cli.database.runtime import CommandRuntime
from cli.start_runtime import StartRuntime
from services.supervision.roster.service_spec import ServiceSpec

# meiva:note
# The browser service runs a headed Chrome on a persistent login profile; the
# operator keeps or stops it explicitly (`--keep-browser`/`--stop-browser`).
_BROWSER_SESSION = "browser"


def _stop_data_plane(
    *,
    skip_infra: bool,
    runner_only: bool,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    producer: Callable[[], EventPipeline],
) -> None:
    """Stop this cluster's own Postgres+Redis (data preserved on disk).

    A runner-only host has no local data plane. `skip_infra` (keep_infra — the
    the fleet update / internal-restart path) leaves the instance running so the
    following migrate/start still has DB; without this the migrate step would hit
    connect-refused now that every cluster (including `main`) owns its instance.
    Otherwise (a full `meiva stop`) the private instance is torn down."""
    if runner_only:
        print("\n→ stop pg/redis: skipped (agent-runner uses the central node)")
    elif skip_infra:
        print("\n→ stop pg/redis: kept up (keep_infra — migrate/start needs DB)")
    else:
        from cli.commands.data_plane.bringup import stop_cluster_instance
        from cli.commands.data_plane.config import data_plane_config_from_settings

        stop_cluster_instance(
            data_plane_config_from_settings(),
            retained_children=retained_children,
            producer=producer,
        )


def reap_cluster_chrome() -> None:
    """Finish a browser teardown the session kill could not: kill any Chrome still
    running on THIS cluster's profile.

    Called only on an explicit force teardown with `keep_browser=False`.
    Normal stop waits for Chrome to exit without escalating signals. After a `SingletonLock` handoff Chrome is no longer a
    descendant of the `meiva-browser` session, so killing that session leaves it
    running on the cluster's CDP port and the next launch's port guard refuses;
    `services/desktop/browser/orphan.py` names it by the cluster's own `--user-data-dir`
    and argues why that can never select a Chrome that is not ours.

    Silent when there is nothing to reap (the common case): the session kill above
    already reported ✓, so an extra "nothing found" line would only be noise.
    Never fails the stop — a teardown that cannot reach the browser must still
    tear the rest of the cluster down, and the pre-existing manual kill remains
    the operator's fallback.
    """
    from services.desktop.browser.orphan import reap_cluster_chrome

    try:
        pids = reap_cluster_chrome()
    except Exception as exc:
        print(f"  ⚠ could not sweep this cluster's Chrome: {exc}", file=sys.stderr)
        return
    if pids:
        print(f"  ✓ reaped Chrome left outside the session: {', '.join(map(str, pids))}")


def _compute_stop_scope(*, keep_infra: bool | None, machine: MachineIdentity) -> tuple[bool, bool]:
    # meiva:note
    # (runner_only, skip_infra) for this stop, from the operator's infra choice.
    #
    # The host's capabilities only refuse a choice that does not apply: a gateway
    # requires one, a host without the gateway capability has no local data plane
    # and accepts none, and unreadable capabilities refuse the stop.
    roles = machine.role()
    _temporary_stop.require_infra_choice(roles, keep_infra=keep_infra)
    runner_only = keep_infra is None
    return runner_only, runner_only or bool(keep_infra)


def _print_stop_plan(
    service_sessions: list[str],
    *,
    keep_shell_sessions: bool,
    keep_browser: bool,
    runner_only: bool,
    keep_infra: bool,
) -> None:
    """The "The following will be stopped" block shown before the confirm gate."""
    # Dynamic lookup for monkeypatch-aware tests.

    print("\nThe following will be stopped:")
    print(f"  service sessions: {', '.join(service_sessions) if service_sessions else '(none)'}")
    if not keep_shell_sessions:
        print("  shell sessions: closed")
    if keep_browser:
        print(f"  browser: kept up ({session_name(_BROWSER_SESSION)}, login session preserved)")
    if runner_only:
        print("  infra (pg/redis): skipped (agent-runner uses the central node)")
    elif keep_infra:
        print("  infra (pg/redis): kept up (cmd_update will migrate next and needs DB)")
    else:
        print("  infra (pg/redis): this cluster's own instance stopped (data preserved on disk)")
    print()


def force_stop(
    _repo: Path,  # retained for the common stop call contract
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
    producer: Callable[[], EventPipeline],
    machine: MachineIdentity,
    helper: HelperFactory | None,
) -> int:
    # meiva:note
    # Explicit force-only resource stop; normal commands use _temporary_stop.
    #
    # Preserves agent identities/data and the selected service/infra/shell-session
    # scope, but may interrupt work. Never entered merely because a graceful stop timed out.
    # Dynamic lookup for monkeypatch-aware tests.
    import cli.commands.lifecycle.root_driver as _root_driver_commands

    _temporary_stop.require_dedicated_choices(preserve_sessions)
    runner_only, skip_infra = _compute_stop_scope(keep_infra=keep_infra, machine=machine)
    root_preserve = preserve_sessions | (
        frozenset({_BROWSER_SESSION}) if keep_browser else frozenset[str]()
    )
    if keep_shell_sessions:
        root_preserve |= {SERVICE_UNIT}
    plan_sessions = _root_driver_commands.root_tree_plan(root_preserve)
    _print_stop_plan(
        plan_sessions,
        keep_shell_sessions=keep_shell_sessions,
        keep_browser=keep_browser,
        runner_only=runner_only,
        keep_infra=bool(keep_infra),
    )
    if not _temporary_stop.confirm_stop(require_confirmation=require_confirmation):
        return 0

    # A deliberate stop must revoke the prior start's recovery authority before
    # any daemon has a chance to observe its own shutdown or a dead peer.
    from base.deploy.lifecycle import start_serving

    start_serving.clear_serving()

    if announce:
        _temporary_stop.announce_stopping(
            machine=machine,
            maintenance_for_seconds=maintenance_for_seconds,
            maintenance_reason=maintenance_reason,
        )
    deadline = deadline_after(timeout)

    # meiva:note
    # Record the browser's tabs before the tree stop below signals the
    # `meiva-browser` session (session_restore owns what is written and why).
    if not keep_browser:
        _temporary_stop.record_browser_session()

    # meiva:note
    # Explicit force interrupts the host process. Agent metadata/checkpoints
    # remain untouched; the next host uses its existing owner recovery. This
    # path leaves no restart behind for the interrupted turns and remains usable offline. The
    # pty-sessions service outlives this step: it closes the shell sessions below.
    _root_driver_commands.stop_root_service_tree(
        preserve=root_preserve | {SERVICE_UNIT},
        timeout_s=remaining(deadline),
        force=True,
        helper=helper,
    )

    # 1.4) a teardown that asked for the browser down finishes the job: kill any
    # Chrome still running on THIS cluster's profile. The session kill above
    # cannot reach a Chrome that left the tree on a `SingletonLock` handoff, and
    # such a Chrome holds the cluster's CDP port against the next launch
    # (services/desktop/browser/orphan.py carries the identification argument). Placed
    # after step 1 on purpose: the watchdog is dead by now, so nothing relaunches
    # Chrome onto the port we just cleared.
    if not keep_browser:
        reap_cluster_chrome()

    # meiva:note
    # Persistent shell sessions are closed through the service that holds them (from its
    # ledger when it is not running), then the service itself goes.
    if not keep_shell_sessions:
        force_close_shell_sessions()
        _root_driver_commands.stop_root_service_tree(
            preserve=root_preserve, timeout_s=remaining(deadline), force=True, helper=helper
        )

    if teardown_extras:
        _temporary_stop.stop_extras(deadline)

    # 2) stop the data plane (data persists on disk).
    _stop_data_plane(
        skip_infra=skip_infra,
        runner_only=runner_only,
        retained_children=retained_children,
        producer=producer,
    )

    return 0


@exclusive_resources
def do_stop(
    _repo: Path,
    *,
    require_confirmation: bool,
    keep_infra: bool | None,
    preserve_sessions: frozenset[str],
    keep_browser: bool,
    keep_shell_sessions: bool,
    announce: bool,
    teardown_extras: bool,
    force: bool,
    timeout: float,
    maintenance_for_seconds: int | None = None,
    maintenance_reason: str | None = None,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    database_factory: DatabaseFactory,
    producer: Callable[[], EventPipeline],
    machine: MachineIdentity,
    helper: HelperFactory | None,
) -> int:
    """Shared stop kernel; only explicit force escalates a service stop."""
    if force:
        return force_stop(
            _repo,
            require_confirmation=require_confirmation,
            keep_infra=keep_infra,
            preserve_sessions=preserve_sessions,
            keep_browser=keep_browser,
            keep_shell_sessions=keep_shell_sessions,
            announce=announce,
            teardown_extras=teardown_extras,
            timeout=timeout,
            maintenance_for_seconds=maintenance_for_seconds,
            maintenance_reason=maintenance_reason,
            retained_children=retained_children,
            producer=producer,
            machine=machine,
            helper=helper,
        )
    return _temporary_stop.stop(
        require_confirmation=require_confirmation,
        keep_infra=keep_infra,
        preserve_sessions=preserve_sessions,
        keep_browser=keep_browser,
        keep_shell_sessions=keep_shell_sessions,
        announce=announce,
        teardown_extras=teardown_extras,
        timeout=timeout,
        maintenance_for_seconds=maintenance_for_seconds,
        maintenance_reason=maintenance_reason,
        retained_children=retained_children,
        database_factory=database_factory,
        producer=producer,
        machine=machine,
        helper=helper,
    )


def cmd_stop(
    *,
    keep_infra: bool | None,
    require_confirmation: bool,
    keep_browser: bool,
    keep_shell_sessions: bool,
    teardown_extras: bool,
    preserve_sessions: frozenset[str],
    force: bool,
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
    # Stop this unit as the operator chose; retain its data.
    #
    # `keep_infra` is None exactly on a host without the gateway capability (no
    # local data plane); every other choice is a required explicit answer.
    with operator_lifecycle_audit(database_factory=database_factory) as command:
        code = do_stop(
            repo_root(),
            require_confirmation=require_confirmation,
            keep_infra=keep_infra,
            preserve_sessions=preserve_sessions,
            keep_browser=keep_browser,
            keep_shell_sessions=keep_shell_sessions,
            announce=True,
            teardown_extras=teardown_extras,
            force=force,
            timeout=timeout,
            maintenance_for_seconds=maintenance_for_seconds,
            maintenance_reason=maintenance_reason,
            retained_children=retained_children,
            database_factory=database_factory,
            producer=producer,
            machine=machine,
            helper=helper,
        )
        command.exited(code)
        return code


def require_restart_runtime(runtime: StartRuntime) -> None:
    """Recheck the captured runtime before disruptive work."""
    from base.paths import prod_service_checkout_error

    runtime.validate()
    if problem := prod_service_checkout_error(runtime.code_root):
        raise ValueError(problem)


def restart_runtime() -> StartRuntime:
    """Admit the currently executing checkout before restart effects."""
    runtime = StartRuntime.development(repo_root())
    require_restart_runtime(runtime)
    return runtime


def _cmd_restart_body(
    *,
    only_services: tuple[str, ...],
    disabled_services: tuple[str, ...],
    all_services: bool,
    keep_shell_sessions: bool,
    keep_browser: bool,
    restart_extras: bool,
    force: bool,
    timeout: float,
    start_timeout: float,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    database_factory: OperatorDatabaseFactory,
    producer: Callable[[], EventPipeline],
    command: CommandRuntime,
) -> int:
    # meiva:note
    # Stop then start the service tree without a stdin confirmation prompt.
    #
    # Hosted agents settle through the agent-host's own SIGTERM exit in the shared stop boundary.
    # Explicit force authorizes interrupting resource shutdown. `timeout` bounds
    # the stop leg; the start leg launches exactly the named service selection.
    if retained_children is None:
        raise ValueError("PostgreSQL launch requires its caller-owned child retention")
    from base.host.exit_codes import RESTART_DECLINED_EXIT_CODE
    from base.host.proc import hosting_exec_domain
    from cli.commands import _repo
    from cli.commands.lifecycle import _start_readiness_preflight, start

    # meiva:note
    # Admission precedes even the restart journal: an incompatible caller must
    # leave the running generation untouched.
    runtime = restart_runtime()

    # meiva:note
    # An exec-domain restart is SIGKILLed by the execute_code call's own
    # teardown — the call's whole process group, as its turn ends (nohup/& do
    # not leave the group — 2026-09-12: macmini stranded mid-stop).
    # Refuse before anything stops. The restart closes persistent shell sessions like a stop,
    # so no shell session can host it either: a plain login shell does.
    exec_domain = hosting_exec_domain()
    if exec_domain is not None:
        print(
            f"  ✗ refusing restart: this process runs inside an agent execute_code "
            f"exec domain ({exec_domain}) — its process group is SIGKILLed when the "
            "call's turn ends, this restart with it — the host stranded mid-restart. Run "
            "it from a shell no meiva session hosts (e.g. a plain ssh/login shell).",
            file=sys.stderr,
        )
        return RESTART_DECLINED_EXIT_CODE

    # Same refusal as `cmd_update`'s in-process legs: the stop below kills every
    # service session's tree, so a restart hosted inside one of them severs
    # itself mid stop→start and leaves the host down. The detached orchestration
    # sessions (the updater ladder's own `meiva restart`) are exempt.
    hosting = hosting_supervised_session()
    if hosting is not None:
        print(
            f"  ✗ refusing restart: this process runs inside supervised session "
            f"{hosting!r}, which the stop leg kills — tree included, this restart with "
            "it, leaving the host stopped. Run it from a shell no meiva session hosts "
            "(e.g. a plain ssh/login shell) — host still serving",
            file=sys.stderr,
        )
        return RESTART_DECLINED_EXIT_CODE

    repo = runtime.code_root
    print(f"[meiva restart] cwd = {repo}")
    from base.deploy.lifecycle import status_journal

    # Only the journal's opener closes it — the same owns_journal contract
    # _temporary_stop keeps (task #2898). When an outer operation's journal is
    # still running, this restart records phases into it and leaves the finish
    # to that caller instead of clobbering its diagnosis.
    owns_journal = status_journal.begin("restart")
    print(
        f"[meiva restart] budget contract: stop up to {timeout:.0f}s + "
        f"start readiness up to {start_timeout:.0f}s + bounded preflight probes; "
        "an outer caller's timeout must exceed this total. Status journal: "
        f"{status_journal.status_path()} (phase progress and the final diagnosis "
        "stay readable there even if this process is cut off)",
        flush=True,
    )

    # Preflight: probe gateway + register machine BEFORE stopping services.
    # A transient gateway outage or network blip would otherwise leave the
    # host in "services dead, can't start" after the stop below.
    # On failure the host keeps serving — abort without stopping.
    print("\n→ preflight probes (validate-before-kill)")
    with status_journal.phase("preflight"):
        rc = _repo.preflight_probes(database_factory(), command=command)
    if rc != 0:
        print("  ✗ refusing restart: preflight probes failed — host still serving", file=sys.stderr)
        if owns_journal:
            status_journal.finish(RESTART_DECLINED_EXIT_CODE, error="preflight probes failed")
        return RESTART_DECLINED_EXIT_CODE

    # Start-readiness preflight (task #3165): the read-only local checks of the
    # start leg below. Same refusal contract as the probes above — nothing was
    # stopped, and rc=3 tells any settled caller "do not start over it" — and
    # every category it refuses on would also fail this restart's own start
    # leg, so a refusal never blocks a viable bounce (the findings name the
    # repairs; a restart could not have made it past them either).
    # `check_launcher=False`: this start is in-process and never execs
    # `.venv/bin/meiva`; the gate's interpreter check covers the venv seam the
    # session launches actually use.
    print("\n→ start readiness preflight (validate-before-kill, local state)")
    with status_journal.phase("preflight"):
        rc = _start_readiness_preflight.preflight_start_readiness(
            repo,
            check_launcher=False,
            runtime=runtime,
            only_services=only_services,
            disabled_services=disabled_services,
            all_services=all_services,
            machine=command.machine,
            host=command.host,
        )
    if rc != 0:
        print(
            "  ✗ refusing restart: start-readiness preflight failed — host still serving; "
            "nothing was stopped. Fix the findings above, then retry.",
            file=sys.stderr,
        )
        if owns_journal:
            status_journal.finish(
                RESTART_DECLINED_EXIT_CODE, error="start-readiness preflight failed"
            )
        return RESTART_DECLINED_EXIT_CODE

    # Every restart uses the shared hosted stop kernel; a timeout never
    # silently authorizes force.
    require_restart_runtime(runtime)

    # meiva:note
    # Restart replaces the services and keeps the private data plane: its start
    # leg needs the database. Shell sessions, the browser and the extras (Gate,
    # the permissions helper, native LGTM) are kept or restarted as the operator
    # chose. A host without the gateway capability has no local data plane.
    keep_infra = True if "gateway" in command.machine.role() else None
    with status_journal.phase("stop"):
        rc = do_stop(
            repo,
            require_confirmation=False,
            keep_infra=keep_infra,
            preserve_sessions=frozenset(),
            keep_browser=keep_browser,
            keep_shell_sessions=keep_shell_sessions,
            announce=False,
            teardown_extras=restart_extras,
            force=force,
            timeout=timeout,
            retained_children=retained_children,
            database_factory=database_factory,
            producer=producer,
            machine=command.machine,
            helper=command.helper,
        )
    if rc != 0:
        # meiva:note
        # The stop leg's own journal phases (services / shell-sessions / ...) remain readable.
        if owns_journal:
            status_journal.finish(rc, error="stop leg failed")
        return rc
    # meiva:note
    # Keep the captured runtime and the operator's selection through startup;
    # neither is recaptured from a later caller or moving selector. Units the
    # stop leg kept still run, so the live root is not reconciled to the selection.
    with status_journal.phase("start"):
        rc = start.cmd_start_body(
            database_factory,
            only_services=only_services,
            disabled_services=disabled_services,
            all_services=all_services,
            reconcile=False,
            ready_timeout=start_timeout,
            runtime=runtime,
            retained_children=retained_children,
            producer=producer,
            command=command,
        )
    if owns_journal:
        status_journal.finish(rc, error=None if rc == 0 else f"start leg failed with rc={rc}")
    return rc


def cmd_restart(
    *,
    only_services: tuple[str, ...],
    disabled_services: tuple[str, ...],
    all_services: bool,
    keep_shell_sessions: bool,
    keep_browser: bool,
    restart_extras: bool,
    force: bool,
    timeout: float,
    start_timeout: float,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    database_factory: OperatorDatabaseFactory,
    producer: Callable[[], EventPipeline],
    command: CommandRuntime,
) -> int:
    # meiva:note
    # Stop then start the service tree without a confirmation prompt.
    with operator_lifecycle_audit(database_factory=database_factory) as audit:
        code = _cmd_restart_body(
            only_services=only_services,
            disabled_services=disabled_services,
            all_services=all_services,
            keep_shell_sessions=keep_shell_sessions,
            keep_browser=keep_browser,
            restart_extras=restart_extras,
            force=force,
            timeout=timeout,
            start_timeout=start_timeout,
            retained_children=retained_children,
            database_factory=database_factory,
            producer=producer,
            command=command,
        )
        audit.exited(code)
        return code


AGENT_HOST = "agent-host"


def _require_ok(response: dict[str, object], what: str) -> None:
    if not response.get("ok"):
        raise root_driver.RootDriverError(
            f"{what} failed: {response.get('code')}: {response.get('error')}"
        )


# meiva:note
# `meiva restart --only agent-host`: stop agent-host and let its root bring it back.
#
# The scope is agent-host alone, so shell sessions, the browser, the extras, every
# other service and the data plane are outside this operation: there is no
# keep-or-stop choice to make. The operator names the interruption mode and the
# stop budget. Agent-host settles its own in-flight turns on SIGTERM; a unit that
# outlives the root's stop window is the root's to kill.
def restart_agent_host(
    *,
    force: bool,
    timeout: float,
    client_for: Callable[[float], Any],
    require_owner: Callable[[dict[str, Any]], None],
    ready: Callable[[ServiceSpec], bool],
) -> int:
    # meiva:note
    # Down agent-host through its live root (`force_down` only with `force`), then up.
    #
    # `client_for(timeout)` opens a root control client, `require_owner` refuses a root
    # this unit does not own and `ready` waits for the unit's protocol readiness.
    from services.supervision.roster import build_services

    client = client_for(timeout)
    status = root_driver.root_status(client)
    if status is None:
        print(
            "  ✗ refusing restart: no live meiva-root on this unit; start it with `meiva start`",
            file=sys.stderr,
        )
        return 1
    require_owner(status)
    if AGENT_HOST not in root_driver.root_units(status):
        print(f"  ✗ refusing restart: the live root runs no {AGENT_HOST} unit", file=sys.stderr)
        return 1
    spec = next(spec for spec in build_services() if spec.session == AGENT_HOST)
    record_lifecycle_request("force-down" if force else "down", [AGENT_HOST])
    _require_ok(
        client.force_down(AGENT_HOST) if force else client.down(AGENT_HOST),
        f"down {AGENT_HOST}",
    )
    print(f"  ✓ {AGENT_HOST} stopped")
    record_lifecycle_request("up", [AGENT_HOST])
    _require_ok(client_for(timeout).up(AGENT_HOST), f"up {AGENT_HOST}")
    if not ready(spec):
        print(f"  ✗ {AGENT_HOST} did not become ready after restart", file=sys.stderr)
        return 1
    print(f"  ✓ {AGENT_HOST} restarted and ready")
    return 0


def cmd_restart_agent_host(
    *,
    force: bool,
    timeout: float,
    start_timeout: float,
    database_factory: OperatorDatabaseFactory,
    command: CommandRuntime,
) -> int:
    # meiva:note
    # Stop agent-host within `timeout` (force-killing only with `force`), then bring it up and
    # wait up to `start_timeout` for its readiness.
    with operator_lifecycle_audit(database_factory=database_factory) as audit:
        try:
            _temporary_stop.refuse_hosted_stop()
            code = restart_agent_host(
                force=force,
                timeout=timeout,
                client_for=lambda seconds: root_driver.root_client(timeout=seconds),
                require_owner=lambda status: root_driver.require_root_owner(
                    status, helper=command.helper
                ),
                ready=lambda spec: (
                    not root_readiness.wait_for_service_tree(
                        (spec,), timeout_s=start_timeout, timers=command.timers
                    ).unready
                ),
            )
        except (RuntimeError, OSError) as exc:
            print(f"  ✗ restart of {AGENT_HOST} incomplete: {exc}", file=sys.stderr)
            code = 1
        audit.exited(code)
        return code
