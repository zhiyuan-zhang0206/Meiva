# meiva:note
# The shell around the one application root: dial it, launch it, reconcile and stop its tree.
#
# What a root status means is decided by `root_control` (the typed snapshot, the reconcile,
# poll and launch plans). This module reads the clock, the control socket and the processes,
# prints, and executes what those plans return. Callers hand it the home as a value, and a
# launch the values it reads from settings (`RootLaunchInputs`); nothing here reads settings.
#
# It reads no process environment: the caller block's mapping is handed in, and every home path
# the module touches is derived from the `home` the caller passes. Nothing below it resolves either
# for itself.

from __future__ import annotations

import json
import signal
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import NamedTuple, TypeGuard, assert_never

import psutil

from base.clock import Deadline, Timers
from base.cluster import session_name
from base.cluster.machine import MachineRoles
from base.deploy import start_trace
from base.deploy.progress_timeout import CRITICAL_SERVICE_SESSIONS
from base.host.atomic_io import write_text_atomic
from base.host.permissions_helper.client import HelperFactory, PermissionsHelperError
from base.host.system.hostport import Host
from base.native_process.ownership import OwnedProcess
from base.native_process.root_control import instance_lock
from base.native_process.root_control.client import (
    RootClient,
    RootClientError,
    caller_identity,
    native_identity,
)
from base.native_process.root_control.ipc import ResponsePayload
from base.paths import root_manifests_path, root_run_dir
from base.sessions.env_forwarding import managed_service_env
from base.sessions.helperproc import HELPER_PROTOCOLS, helper_wire_ok
from cli.commands.data_plane.instance.launch_delivery import LaunchDelivery
from cli.commands.lifecycle.root_control.generation import launch_digest
from cli.commands.lifecycle.root_control.launch_plan import (
    RootLaunch,
    RootLaunchInputs,
    keeper_seed,
    plan_root_launch,
)
from cli.commands.lifecycle.root_control.poll_plan import (
    Expired,
    KeeperPending,
    KeeperRefused,
    KeeperRunning,
    Retry,
    keeper_verdict,
    unmet_step,
)
from cli.commands.lifecycle.root_control.reconcile_plan import (
    LeaveTree,
    StopUnits,
    changed_inputs,
    classify_launch,
    generation_refusal,
    plan_resume,
    plan_tree_stop,
)
from cli.commands.lifecycle.root_control.snapshot import (
    RootSnapshot,
    SnapshotError,
    parse_snapshot,
    root_units,
)
from cli.database.lifecycle_audit import record_lifecycle_request
from cli.start_runtime import StartRuntime
from services.desktop.permissions_helper.launchd_job import BoundedRunner, LaunchdFacts
from services.supervision.roster import service_spec
from services.supervision.roster.service_spec import ServiceSpec

_ROOT_SOCKET_NAME = "meiva-root.sock"

# Root binds IPC after spawning its units.
_ROOT_READY_TIMEOUT_S = 30.0
# Unit stop windows accumulate in dependency order.
_ROOT_STOP_TIMEOUT_S = 90.0
READY_POLL_INTERVAL_S = 0.5


class RootDriverError(RuntimeError):
    """The root-driven tree could not be brought up, reconciled or stopped."""


def complete_boot_start(
    *,
    helper: HelperFactory | None,
    host: Host,
    home: Path,
    control_pid: int,
    proc_root: Path = Path("/proc"),
) -> None:
    """Hand Linux systemd the verified root after ordinary readiness succeeds."""
    from base.host.system.boot_unit import in_boot_unit, publish_root_ready

    if not in_boot_unit(host=host, pid=control_pid, proc_root=proc_root):
        return
    snapshot = root_status(root_client(home, environ=host.environ))
    if snapshot is None:
        raise RootDriverError("cannot hand systemd an unobservable root")
    require_root_owner(snapshot, helper=helper, home=home)
    ready = native_identity(snapshot["root"])
    publish_root_ready(home, ready, control_pid, host=host, proc_root=proc_root)


class LaunchOutcome(NamedTuple):
    """The complete requested roster and units whose launch failed."""

    started: tuple[ServiceSpec, ...]
    failed: tuple[str, ...]


def service_extra_env(spec: ServiceSpec, *, delivery: LaunchDelivery) -> dict[str, str]:
    """Bind profile, database login and API token to one service, never its parent."""
    from base.telemetry.lgtm_local import BACKENDS, service_environment
    from cli.commands.data_plane.bringup import api_delivery, db_delivery, runner_db_url

    extra = service_environment(spec.session) if spec.session in BACKENDS else {}
    marker = service_spec.profile_marker(spec)
    if marker is not None:
        extra["MEIVA_PROCESS_PROFILE"] = marker
    cls = service_spec.db_access(spec)
    db_env = db_delivery(cls, delivery=delivery) if cls is not None else {}
    if db_env:
        extra.update(db_env)
    elif marker == "agent":
        extra["MEIVA_DB_URL"] = runner_db_url(delivery)
    if (api := service_spec.api_access(spec)) is not None:
        extra.update(api_delivery(api, delivery=delivery))
    if spec.plugin_config is not None:
        from base.host.env.registry import SERVICE_PLUGIN_CONFIG_ENV
        from base.packages.plugins.config_registration import service_config_packet

        extra[SERVICE_PLUGIN_CONFIG_ENV] = service_config_packet(*spec.plugin_config)
    return extra


# meiva:note
# The capability/config-selected units this root owns, minus the explicit skips: `annotated` is the
# roster of this host's roles with each gated-out unit's reason.
def start_roster(
    annotated: tuple[tuple[ServiceSpec, str | None], ...], launch_skip: set[str]
) -> tuple[ServiceSpec, ...]:
    return tuple(
        spec for spec, reason in annotated if reason is None and spec.session not in launch_skip
    )


# meiva:note
# A blocking client bound to `home`'s root control socket.
#
# The client carries the caller block of this process, which the root records beside every
# state verb it serves (`base/native_process/root_control/docs/lifecycle-caller.meiva.okf.md`):
# pid, working directory and argv are facts of the process; `environ` is the mapping handed in.
def root_client(home: Path, *, environ: Mapping[str, str], timeout: float = 5.0) -> RootClient:
    return RootClient(
        root_run_dir(home) / _ROOT_SOCKET_NAME,
        caller=caller_identity(environ),
        timeout=timeout,
    )


# meiva:note
# The daemon's validated status snapshot, or None when no root answers (yet).
#
# A root that answers with a snapshot the commands cannot read is not "no root": it is a
# `RootDriverError`, so every shell that already reports a failed root call (a launch, a stop,
# a restart) reports this one, and the shells that must run on a broken host (`meiva status`,
# the readiness poll) catch it where they read the snapshot.
def root_status(client: RootClient) -> RootSnapshot | None:
    try:
        response = client.status()
    except RootClientError:
        return None
    if not response["ok"]:
        return None
    try:
        return parse_snapshot(response.get("result"))
    except SnapshotError as exc:
        raise RootDriverError(f"meiva-root answered with an unreadable status: {exc}") from exc


def _call_ok(response: ResponsePayload, what: str) -> None:
    """Raise `RootDriverError` when a K1 reply is not ok."""
    if not response["ok"]:
        raise RootDriverError(f"{what} failed: {response.get('code')}: {response.get('error')}")


def require_root_owner(snapshot: RootSnapshot, *, helper: HelperFactory | None, home: Path) -> None:
    """Bind every reused or stopped root to its required live native parent."""
    root = native_identity(snapshot["root"])
    if not root.live():
        raise RootDriverError("recorded root native birth is no longer live")
    if helper is None:
        return
    ping = helper().ping()
    if not all(ping.get(capability) is True for capability in HELPER_PROTOCOLS):
        raise RootDriverError(f"required helper lacks lifecycle protocols: {HELPER_PROTOCOLS}")
    helper_pid = ping.get("pid")
    if isinstance(helper_pid, bool) or not isinstance(helper_pid, int) or helper_pid <= 1:
        raise RootDriverError("required helper omitted its native process")
    helper_process = OwnedProcess.capture(psutil.Process(helper_pid))
    keeper = helper().root_status()
    if (
        keeper.get("pid") != root.pid
        or keeper.get("run_dir") != str(root_run_dir(home))
        or psutil.Process(root.pid).ppid() != helper_process.pid
        or not helper_process.live()
        or not root.live()
    ):
        raise RootDriverError(
            f"root is outside this home's signed-helper custody: live root pid {root.pid} was "
            "started by a previous permissions helper, so this home's keeper neither adopts it "
            "nor seeds a replacement while it holds the instance lock. That root retires itself "
            "once it sees its helper ancestry is gone; wait for it to exit, then run "
            "`meiva start` again (task #5292)."
        )


def tree_manifest(
    roster: tuple[ServiceSpec, ...],
    repo: Path,
    *,
    roles: MachineRoles,
    delivery: LaunchDelivery,
    home: Path,
) -> dict[str, object]:
    """Prepare launch inputs without changing any running generation's seed."""
    from services.supervision.meiva_root_glue.manifests import build_manifest

    return build_manifest(
        capabilities=sorted(roles),
        repo_root=repo,
        specs=roster,
        environments={s.session: service_extra_env(s, delivery=delivery) for s in roster},
        home=home,
    )


# meiva:note
# The tree manifest bound to its launch digest: the exact inputs of one launch.
def launch_manifest(
    roster: tuple[ServiceSpec, ...],
    repo: Path,
    *,
    roles: MachineRoles,
    env: Mapping[str, str],
    inputs: RootLaunchInputs,
) -> dict[str, object]:
    manifest = tree_manifest(roster, repo, roles=roles, delivery=inputs.delivery, home=inputs.home)
    manifest["launch_digest"] = launch_digest(repo, env, home=inputs.home)
    return manifest


def _log_tail(path: Path, lines: int = 20) -> str:
    """The last `lines` of a log file, for an actionable bring-up failure."""
    try:
        content = path.read_text(errors="replace").splitlines()
    except OSError:
        return "(no log)"
    return "\n".join(content[-lines:]) or "(empty log)"


def spawn_direct(
    launch: RootLaunch,
    env: Mapping[str, str],
    *,
    retained_children: list[subprocess.Popen[bytes]],
) -> subprocess.Popen[bytes]:
    """Launch the root daemon detached (own session), logging under the run dir."""
    with launch.stdout_log.open("ab") as stdout, launch.stderr_log.open("ab") as stderr:
        child = subprocess.Popen(
            launch.argv,
            cwd=launch.cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            close_fds=True,
        )
        # The CLI caller retains its direct children through its final telemetry drain.
        # Dropping this handle would let a later Popen reap a published root PID.
        retained_children.append(child)
    return child


def await_root_status(
    client: RootClient,
    launch: RootLaunch,
    *,
    timers: Timers,
    proc: subprocess.Popen[bytes] | None = None,
    timeout_s: float = _ROOT_READY_TIMEOUT_S,
    interval_s: float = READY_POLL_INTERVAL_S,
) -> RootSnapshot:
    """Poll the control socket until the daemon serves, or fail with its log tail."""
    started = timers.monotonic()
    while True:
        if proc is not None and proc.poll() is not None:
            raise RootDriverError(
                f"meiva-root exited before serving (rc={proc.returncode}); "
                f"log tail:\n{_log_tail(launch.stderr_log)}"
            )
        snapshot = root_status(client)
        if snapshot is not None:
            return snapshot
        unmet = unmet_step(
            elapsed_s=timers.monotonic() - started,
            timeout_s=timeout_s,
            interval_s=interval_s,
        )
        match unmet:
            case Retry(after_s):
                timers.sleep(after_s)
            case Expired():
                raise RootDriverError(
                    f"meiva-root did not become ready within {timeout_s:.0f}s; "
                    f"log tail:\n{_log_tail(launch.stderr_log)}"
                )
            case _:
                assert_never(unmet)


def seed_via_helper(
    launch: RootLaunch,
    env: Mapping[str, str],
    *,
    helper: HelperFactory,
    timers: Timers,
    timeout_s: float = _ROOT_READY_TIMEOUT_S,
    interval_s: float = READY_POLL_INTERVAL_S,
) -> None:
    """Seed the root keeper and wait for it to report the root `running`."""
    seed = keeper_seed(launch, env)
    try:
        write_text_atomic(
            launch.run_dir / "seed.json", json.dumps(seed), mode=0o600, sync_parent=True
        )
        keeper = helper().seed_root(seed)
    except Exception as exc:
        raise RootDriverError(f"root_seed over the helper failed: {exc}") from exc
    started = timers.monotonic()
    while True:
        verdict = keeper_verdict(keeper, str(launch.run_dir))
        match verdict:
            case KeeperRunning():
                return
            case KeeperRefused(reason):
                raise RootDriverError(reason)
            case KeeperPending():
                pass
            case _:
                assert_never(verdict)
        unmet = unmet_step(
            elapsed_s=timers.monotonic() - started,
            timeout_s=timeout_s,
            interval_s=interval_s,
        )
        match unmet:
            case Retry(after_s):
                timers.sleep(after_s)
            case Expired():
                raise RootDriverError(
                    f"the helper did not bring meiva-root up within {timeout_s:.0f}s "
                    f"(keeper state={keeper['state']})"
                )
            case _:
                assert_never(unmet)
        try:
            keeper = helper().root_status()
        except Exception as exc:
            raise RootDriverError(f"root_status over the helper failed: {exc}") from exc


def bring_up_root(
    launch: RootLaunch,
    client: RootClient,
    env: Mapping[str, str],
    *,
    retained_children: list[subprocess.Popen[bytes]],
    helper: HelperFactory | None,
    timers: Timers,
) -> RootSnapshot:
    """Start through the required platform owner; never change ownership on failure."""
    if helper is not None:
        if not helper_wire_ok(helper):
            raise RootDriverError(
                f"the macOS helper is unreachable or lacks {HELPER_PROTOCOLS}; prepare and "
                "activate a reviewed signed helper supporting durable root stop before "
                "starting services. Refusing direct spawning outside the permission ancestry"
            )
        seed_via_helper(launch, env, helper=helper, timers=timers)
        return await_root_status(client, launch, timers=timers)
    proc = spawn_direct(launch, env, retained_children=retained_children)
    print(f"  + meiva-root spawned directly (pid {proc.pid})")
    return await_root_status(client, launch, proc=proc, timers=timers)


def stop_root_process(
    client: RootClient,
    snapshot: RootSnapshot,
    *,
    timeout_s: float,
    helper: HelperFactory | None,
    home: Path,
    timers: Timers,
    interval_s: float = READY_POLL_INTERVAL_S,
) -> None:
    """Stop through the keeper on macOS, or SIGTERM; await native root exit."""
    require_root_owner(snapshot, helper=helper, home=home)
    identity = native_identity(snapshot["root"])
    if helper is not None:
        if not helper_wire_ok(helper):
            raise RootDriverError(
                "the required macOS helper is unreachable — refusing to "
                "signal a helper-seeded root directly (the keeper would restart it); stop it "
                "through the helper"
            )
        try:
            reply = helper().stop_root()
        except Exception as exc:
            raise RootDriverError(f"root_stop over the helper failed: {exc}") from exc
        if reply["stop_requested"] is not True:
            raise RootDriverError("helper did not retain root stop intent")
    else:
        identity.send_signal(signal.SIGTERM)
    started = timers.monotonic()
    while True:
        if not identity.live() and root_status(client) is None:
            return
        unmet = unmet_step(
            elapsed_s=timers.monotonic() - started,
            timeout_s=timeout_s,
            interval_s=interval_s,
        )
        match unmet:
            case Retry(after_s):
                timers.sleep(after_s)
            case Expired():
                raise RootDriverError(
                    f"meiva-root at {root_run_dir(home)} did not stop within {timeout_s:.0f}s"
                )
            case _:
                assert_never(unmet)


# meiva:note
# Bring up every requested unit that is down, within the same root generation.
#
# An explicit operator start resumes a stopped unit; the OS boot job's retried start
# (`honor_restart_budget`) leaves a unit down whose circuit breaker is open, because the
# health monitor keeps retrying it and the boot job must not race it (#5315, #4872).
def reconcile_units(
    roster: tuple[ServiceSpec, ...],
    client: RootClient,
    snapshot: RootSnapshot,
    *,
    honor_restart_budget: bool,
) -> RootSnapshot:
    plan = plan_resume(
        [spec.session for spec in roster], snapshot, honor_restart_budget=honor_restart_budget
    )
    for unit_id in plan.held_down:
        print(
            f"  · {session_name(unit_id)} has an open restart breaker — "
            "the health monitor keeps retrying it"
        )
    if plan.up:
        record_lifecycle_request("up", [resume.unit_id for resume in plan.up])
    for resume in plan.up:
        print(
            f"  ↑ meiva-root unit {session_name(resume.unit_id)} ({resume.reason}) — bringing it up"
        )
        try:
            _call_ok(client.up(resume.unit_id), f"up {resume.unit_id}")
        except RootDriverError as exc:
            if resume.unit_id in CRITICAL_SERVICE_SESSIONS:
                raise
            print(f"  ✗ optional service {resume.unit_id}: {exc}", file=sys.stderr)
    refreshed = root_status(client)
    return refreshed if refreshed is not None else snapshot


# meiva:note
# The launch verdict: units that should run but could not be spawned.
def _launch_outcome(roster: tuple[ServiceSpec, ...], snapshot: RootSnapshot) -> LaunchOutcome:
    problems = classify_launch([spec.session for spec in roster], snapshot)
    for problem in problems:
        print(f"  ✗ {session_name(problem.unit_id)}: {problem.detail}", file=sys.stderr)
    return LaunchOutcome(roster, tuple(session_name(problem.unit_id) for problem in problems))


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


# meiva:note
# The requested manifest digest of every unit, by unit id.
def unit_digests(manifest: Mapping[str, object]) -> dict[str, str]:
    from services.supervision.meiva_root.manifest import UnitManifest

    rows = manifest["units"]
    if not _is_object_list(rows):
        raise TypeError("the launch manifest carries no unit list")
    desired = (
        UnitManifest.from_mapping(row, origin="requested tree")
        for row in rows
        if _is_object_dict(row)
    )
    return {unit.id: unit.digest() for unit in desired}


# meiva:note
# Refuse a running root whose generation differs from the requested launch.
def require_same_generation(
    manifest: Mapping[str, object], snapshot: RootSnapshot, *, reconcile: bool
) -> None:
    digest = manifest["launch_digest"]
    if not isinstance(digest, str):
        raise TypeError("the launch manifest carries no launch digest")
    changed = changed_inputs(unit_digests(manifest), digest, snapshot, reconcile=reconcile)
    if changed:
        raise RootDriverError(generation_refusal(changed))


def admit_live_start(
    roster: tuple[ServiceSpec, ...],
    repo: Path,
    roles: MachineRoles,
    *,
    reconcile: bool,
    helper: HelperFactory | None,
    inputs: RootLaunchInputs,
) -> bool:
    """Observe before any converge/schema write; reuse only identical inputs."""
    snapshot = root_status(root_client(inputs.home, environ=inputs.environment))
    if snapshot is None:
        require_root_absent(inputs.home)
        return False
    require_root_owner(snapshot, helper=helper, home=inputs.home)
    env = managed_service_env(inputs.service_path, environ=inputs.environment)
    manifest = launch_manifest(roster, repo, roles=roles, env=env, inputs=inputs)
    require_same_generation(manifest, snapshot, reconcile=reconcile)
    return True


# meiva:note
# The cold path's one ordering fact: the operator's `up` row precedes the published
# manifest and the spawned root, so a bring-up that dies still names who asked for it
# (task #5535).
def bring_up_cold_root(
    roster: tuple[ServiceSpec, ...],
    launch: RootLaunch,
    manifest: Mapping[str, object],
    client: RootClient,
    env: Mapping[str, str],
    *,
    retained_children: list[subprocess.Popen[bytes]],
    helper: HelperFactory | None,
    timers: Timers,
) -> RootSnapshot:
    from services.supervision.meiva_root_glue.manifests import write_manifest

    record_lifecycle_request("up", [spec.session for spec in roster])
    write_manifest(launch.manifests, manifest)
    with start_trace.phase("root spawn"):
        return bring_up_root(
            launch, client, env, retained_children=retained_children, helper=helper, timers=timers
        )


# meiva:note
# Start the requested services through their root owner.
#
# An identical running generation is reused; changing its inputs requires a prior stop.
def launch_service_tree(
    roster: tuple[ServiceSpec, ...],
    repo: Path,
    roles: MachineRoles,
    *,
    reconcile: bool,
    retained_children: list[subprocess.Popen[bytes]],
    helper: HelperFactory | None,
    runtime: StartRuntime,
    honor_restart_budget: bool,
    inputs: RootLaunchInputs,
    timers: Timers,
) -> LaunchOutcome:
    home = inputs.home
    env = managed_service_env(inputs.service_path, environ=inputs.environment)
    with start_trace.phase("launch manifest"):
        manifest = launch_manifest(roster, repo, roles=roles, env=env, inputs=inputs)
    launch = plan_root_launch(
        interpreter=runtime.interpreter,
        cwd=runtime.cwd,
        run_dir=root_run_dir(home),
        manifests=root_manifests_path(home),
    )
    client = root_client(home, environ=inputs.environment)
    try:
        snapshot = root_status(client)
        if snapshot is not None:
            require_root_owner(snapshot, helper=helper, home=home)
            require_same_generation(manifest, snapshot, reconcile=reconcile)
        _ensure_frontend_deps(roster, snapshot, repo, environment=inputs.environment)
        if snapshot is None:
            require_root_absent(home)
            snapshot = bring_up_cold_root(
                roster,
                launch,
                manifest,
                client,
                env,
                retained_children=retained_children,
                helper=helper,
                timers=timers,
            )
            require_root_owner(snapshot, helper=helper, home=home)
            require_same_generation(manifest, snapshot, reconcile=reconcile)
        with start_trace.phase("units up"):
            snapshot = reconcile_units(
                roster, client, snapshot, honor_restart_budget=honor_restart_budget
            )
    except RootDriverError as exc:
        print(f"  ✗ meiva-root tree bring-up failed: {exc}", file=sys.stderr)
        return LaunchOutcome(roster, tuple(session_name(spec.session) for spec in roster))
    return _launch_outcome(roster, snapshot)


_NPM_CI_ARGV = ("npm", "ci")


# meiva:note
# Install the frontend's dependencies when the frontend is requested and not running.
def _ensure_frontend_deps(
    roster: tuple[ServiceSpec, ...],
    snapshot: RootSnapshot | None,
    repo: Path,
    *,
    environment: Mapping[str, str],
) -> None:
    if not any(spec.session == "frontend" for spec in roster):
        return
    frontend = None if snapshot is None else root_units(snapshot).get("frontend")
    if frontend is not None and frontend["state"] == "running":
        return
    from cli.commands._repo import ensure_frontend_deps

    ensure_frontend_deps(repo, environ=environment, install_argv=_NPM_CI_ARGV)


# meiva:note
# Positive proof that no root holds `home`'s instance lock.
def require_root_absent(home: Path) -> None:
    fd = instance_lock.acquire_instance_lock(root_run_dir(home))
    instance_lock.release_instance_lock(fd)


def root_tree_plan(
    preserve: frozenset[str], *, home: Path, environ: Mapping[str, str]
) -> list[str]:
    """Display names for the selected exact root units."""
    selection = root_tree_selection(home, environ=environ)
    return sorted(name for name, unit in selection.items() if unit not in preserve)


def root_tree_selection(home: Path, *, environ: Mapping[str, str]) -> dict[str, str]:
    """Map the home's qualified display names to the root's exact unit IDs."""
    snapshot = root_status(root_client(home, environ=environ))
    if snapshot is None:
        require_root_absent(home)
        return {}
    return {session_name(unit_id): unit_id for unit_id in root_units(snapshot)}


def stop_dormant_helper_root(
    deadline: float,
    *,
    helper: HelperFactory | None,
    home: Path,
    facts: LaunchdFacts,
    run: BoundedRunner,
    timers: Timers,
) -> None:
    """Revoke a pending keeper restart even when no root IPC is serving."""
    if helper is None:
        return
    from services.desktop.permissions_helper.launchd_job import helper_job_label, retirement_query

    try:
        keeper = helper().root_status()
    except PermissionsHelperError as exc:
        target = f"{facts.domain}/{helper_job_label(home)}"
        if retirement_query(target, deadline, run=run, timers=timers) is None:
            return
        raise RootDriverError(
            "loaded helper custody is unavailable; stop remains incomplete"
        ) from exc
    if keeper["state"] == "unseeded" and not keeper["seeded"]:
        return
    if keeper.get("pid") is not None or keeper["state"] == "conflict":
        raise RootDriverError("helper owns a root without usable IPC; custody requires recovery")
    if not helper_wire_ok(helper) or helper().stop_root()["stop_requested"] is not True:
        raise RootDriverError("helper did not persist stop of its dormant root")
    require_root_absent(home)


# meiva:note
# Units go down one at a time in root's declaration order, the order root's own
# whole-tree stop uses, so the agent-host the manifest declares first is stopped first.
# Root answers `down` only once the unit has exited (or been killed at the end of its
# TERM window), so nothing else is stopped while the agent-host drains. Each call dials a
# client bounded by the time left of the shared deadline.
# meiva:link services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md
def down_units(
    unit_ids: Iterable[str],
    *,
    deadline: Deadline,
    force: bool,
    home: Path,
    environ: Mapping[str, str],
) -> None:
    for unit_id in unit_ids:
        remaining_s = deadline.left()
        if remaining_s is None:
            raise RootDriverError("service stop deadline expired; custody retained")
        client = root_client(home, environ=environ, timeout=remaining_s)
        response = client.force_down(unit_id) if force else client.down(unit_id)
        _call_ok(response, f"down {unit_id}")
        print(f"  ✓ meiva-root unit {session_name(unit_id)} stopped")


def stop_root_service_tree(
    *,
    preserve: frozenset[str],
    timeout_s: float = _ROOT_STOP_TIMEOUT_S,
    force: bool = False,
    helper: HelperFactory | None,
    home: Path,
    facts: LaunchdFacts,
    run: BoundedRunner,
    timers: Timers,
    environ: Mapping[str, str],
) -> None:
    """Stop the root-owned tree — everything, or only the units not preserved.

    `preserve` holds bare service names (restart's browser, `--keep-service`).
    Stopping every unit also stops the root itself; keeping at least one unit
    leaves the root running to host it. Raises `RootDriverError` when the
    tree cannot be brought down; the caller's phase accounting reports it.
    """
    deadline = Deadline.after(timers, timeout_s)
    client = root_client(home, environ=environ, timeout=timeout_s)
    snapshot = root_status(client)
    if snapshot is None:
        require_root_absent(home)
        stop_dormant_helper_root(
            deadline.at, helper=helper, home=home, facts=facts, run=run, timers=timers
        )
        print("  meiva-root: no live owner or retained service custody")
        return
    require_root_owner(snapshot, helper=helper, home=home)
    tree_stop = plan_tree_stop(root_units(snapshot), preserve)
    match tree_stop:
        case LeaveTree():
            print("  meiva-root: every unit is preserved — tree left running")
        case StopUnits(unit_ids, stop_root):
            record_lifecycle_request("force-down" if force else "down", unit_ids)
            down_units(unit_ids, deadline=deadline, force=force, home=home, environ=environ)
            if stop_root:
                print(
                    f"  meiva-root: stopping the whole tree ({len(unit_ids)} unit(s)) and the root"
                )
                stop_root_process(
                    client,
                    snapshot,
                    timeout_s=max(0.0, deadline.at - timers.monotonic()),
                    helper=helper,
                    home=home,
                    timers=timers,
                )
                print("  ✓ meiva-root stopped (tree down, root exited)")
        case _:
            assert_never(tree_stop)
