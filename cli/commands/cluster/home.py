# meiva:note
# Decommission of the host's cluster; initialization belongs only to start.
#
# `meiva cluster destroy` is a shell around a plan. The shell reads what the home records and
# what this host is, `plans.destroy_plan` decides what to ask, say and run, and the steps are
# executed here in order, stopping at the first one that fails so a half-torn-down home stays
# attached with its destroy intent retained.

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from base.cluster.dataplane.pg_admin import pg_socket_path
from base.host.permissions_helper.client import PermissionsHelperClient
from base.host.private_storage import write_private_bytes
from base.host.system.boot_autostart.registration import unregister_autostart
from base.host.system.cron.health_job import unregister_os_cron
from base.host.system.hostport import Host
from base.host.system.logs_job import unregister_logs_job
from base.host.system.packages_job import unregister_packages_job
from base.host.system.pr_flow_job import unregister_pr_flow_job
from base.host.system.walg_job import unregister_walg_job
from base.native_process.os_platform import file_lock
from cli.commands.cluster.plans.destroy_plan import (
    CONFIRMATION_PROMPT,
    NO_TERMINAL_REFUSAL,
    STOP_INCOMPLETE,
    DestroyFacts,
    DestroyStep,
    DropData,
    HostSystem,
    PublishIntent,
    RetireHelper,
    RetireJobs,
    StopCluster,
    StopMode,
    answer_confirms,
    cleanup_incomplete,
    destroy_prompt,
    plan_destroy,
    scheduled_jobs_failure,
)
from cli.database.runtime import Terminal
from cli.start_identity import read_intent
from services.desktop.permissions_helper.launchd_job import unregister_helper


# meiva:note
# What the operator asked destroy to do, with the facts of this checkout and configuration
# that the composition root reads once: the home it acts on (this process's home, the default
# production home included), the checkout whose `meiva` runs the stop, and the registered
# permissions-helper port. The terminal the person answers at travels beside it, in the
# command's runtime.
@dataclass(frozen=True)
class DestroyRequest:
    home: Path
    drop_db: bool
    stop_mode: StopMode
    stop_timeout_s: float
    checkout: Path
    helper_port: int


# meiva:note
# What `--drop-db` deletes: the cluster's own data directories and the short Postgres socket
# directory under /tmp. One list for the prompt and the removal, so the prompt names exactly
# what goes.
def _drop_dirs(home: Path) -> tuple[Path, ...]:
    return (home / "pg", home / "redis", pg_socket_path(home))


# meiva:note
# Detach the host's cluster: stop it, retire its OS jobs and helper, mark the home detached,
# optionally remove its data dirs.
#
# Returns 0 on success, 1 when the home holds no start intent, the confirmation is not given,
# or a step fails (a failed stop returns the stop's own status).
#
# Without `--drop-db` the home's own files stay (`.env` included): a detached home's `.env`
# is the only copy of that cluster's secret, of any key hand-added beyond `SEED_ENV_KEYS`, and
# of the URLs its data-plane identity is read from. The `detached` destroy intent is what
# stops the leftover home from starting again (`cli.start_identity.prepare_identity` refuses
# it); deleting that file by hand is the only way back.
def cmd_cluster_destroy(request: DestroyRequest, *, host: Host, terminal: Terminal) -> int:
    home = request.home.resolve()
    intent = read_intent(home)
    if intent is None:
        print(f"✗ meiva cluster destroy: no start intent at '{home}'", file=terminal.stderr)
        return 1
    facts = DestroyFacts(
        home=str(home),
        roles=tuple(intent["roles"]),
        stop_mode=request.stop_mode,
        stop_timeout_s=request.stop_timeout_s,
        meiva_executable=str(request.checkout / ".venv" / "bin" / "meiva"),
        platform=HostSystem(host.platform.system),
        helper_port=request.helper_port,
        drop_dirs=tuple(str(directory) for directory in _drop_dirs(home))
        if request.drop_db
        else (),
    )
    if not _confirmed(facts, terminal):
        return 1
    plan = plan_destroy(facts)
    # meiva:note
    # Publish a terminal intent before stopping. Concurrent/internal starts must refuse it from
    # the first moment of the teardown.
    with file_lock(home / "start-intent.lock", timeout_s=30):
        for step in plan.under_lock:
            failed = _execute(step, request, home=home, host=host, terminal=terminal)
            if failed is not None:
                return failed
    for step in plan.afterwards:
        _execute(step, request, home=home, host=host, terminal=terminal)
    return 0


# meiva:note
# Ask a person at a terminal to type the home path; True only when they did.
#
# Anything else (no terminal, end of input, a different string) does nothing.
def _confirmed(facts: DestroyFacts, terminal: Terminal) -> bool:
    if not terminal.interactive:
        print(NO_TERMINAL_REFUSAL, file=terminal.stderr)
        return False
    for line in destroy_prompt(facts):
        print(line, file=terminal.stdout)
    terminal.stdout.write(CONFIRMATION_PROMPT)
    terminal.stdout.flush()
    if not answer_confirms(terminal.stdin.readline(), facts.home):
        print("aborted: nothing was changed", file=terminal.stderr)
        return False
    return True


# meiva:note
# Perform one step; the exit status to stop with when it failed, else None.
def _execute(
    step: DestroyStep, request: DestroyRequest, *, home: Path, host: Host, terminal: Terminal
) -> int | None:
    match step:
        case PublishIntent():
            write_private_bytes(home / "destroy-intent.json", step.marker())
            if step.announce is not None:
                print(step.announce, file=terminal.stdout)
        case StopCluster(argv):
            returncode = subprocess.run(argv, cwd=request.checkout, check=False).returncode
            if returncode != 0:
                print(STOP_INCOMPLETE, file=terminal.stderr)
                return returncode
        case RetireJobs():
            try:
                unregister_scheduled_jobs(host=host)
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                print(cleanup_incomplete(str(exc)), file=terminal.stderr)
                return 1
        case RetireHelper(helper_port):
            try:
                unregister_helper(home, helper_port=helper_port, connect=PermissionsHelperClient)
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                print(cleanup_incomplete(str(exc)), file=terminal.stderr)
                return 1
        case DropData(directories, announce):
            # meiva:note
            # The whole Postgres+Redis instance is this cluster's own, so removing its data dirs
            # IS the drop: there is no shared server to DROP DATABASE inside. The stop already
            # stopped it.
            for directory in directories:
                shutil.rmtree(directory, ignore_errors=True)
            print(announce, file=terminal.stdout)
        case _:
            assert_never(step)
    return None


# meiva:note
# Remove every OS-scheduled job the host registered (health probe, boot autostart, logs
# maintenance, packages refresh, PR flow and the WAL-G tick).
#
# Labels name jobs, not homes, and each `unregister_*` is a no-op outside the default home, so
# a scratch home's destroy never touches the host's jobs. Every job must be retired before the
# home is marked detached. An unavailable scheduler is ambiguous custody, so failures are
# raised.
def unregister_scheduled_jobs(*, host: Host) -> None:
    jobs = (
        ("health probe", unregister_os_cron),
        ("autostart", unregister_autostart),
        ("logs maintenance", unregister_logs_job),
        ("packages refresh", unregister_packages_job),
        ("PR flow", unregister_pr_flow_job),
        ("WAL-G tick", unregister_walg_job),
    )
    failed: list[tuple[str, str]] = []
    for name, unregister in jobs:
        try:
            unregister(host=host)
        except Exception as e:
            failed.append((name, str(e)))
    if failed:
        raise RuntimeError(scheduled_jobs_failure(failed))
