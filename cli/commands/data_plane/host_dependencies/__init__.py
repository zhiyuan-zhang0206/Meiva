# meiva:note
# The host-dependency gate: the first cold-start converge step.
#
# A shell around a plan. `readings.py` looks at the host once per required tool, `diagnosis.py`
# says which are missing and why, `gate_plan.py` decides to pass, install groups or refuse and
# words each report, and `install_plan.py` words and orders the installs. This module prints the
# lines, runs the provisioning scripts through the host and reads the host again after them.
# meiva:link cli/commands/lifecycle/docs/start-host-dependencies.meiva.okf.md

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import assert_never

from base.deploy.progress_timeout import HOST_PACKAGE_INSTALL_TIMEOUT_S
from base.host.system.cron.mechanics import owns_os_job_home
from base.host.system.hostport import Host
from base.host.system.job_values import JobRoots
from base.paths.home import is_prod_install
from base.sessions.env_forwarding import frontend_toolchain_path
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.data_plane.config import data_plane_config_from_boot
from cli.commands.data_plane.host_dependencies.diagnosis import (
    DependencyProblem,
    Reading,
    missing_tools,
)
from cli.commands.data_plane.host_dependencies.gate_plan import (
    InstallGroups,
    Ready,
    Refuse,
    Satisfied,
    plan_gate,
    plan_recheck,
)
from cli.commands.data_plane.host_dependencies.install_plan import (
    exit_line,
    launch_failure_line,
    missing_script_line,
    plan_install,
)
from cli.commands.data_plane.host_dependencies.readings import Probing, read_tools
from cli.commands.data_plane.host_dependencies.scope import PlaneFacts, StartScope
from cli.commands.data_plane.host_dependencies.table import (
    HOST_TOOLS,
    Component,
    render_table,
    required_tools,
)

__all__ = [
    "HOST_TOOLS",
    "DependencyProblem",
    "PlaneFacts",
    "Probing",
    "Reading",
    "StartScope",
    "ensure_host_dependencies",
    "ensure_host_dependencies_step",
    "missing_tools",
    "probing_for",
    "render_table",
    "required_tools",
    "scope_for",
]


def _emit(lines: tuple[str, ...], *, err: bool = False) -> None:
    for line in lines:
        print(line, file=sys.stderr if err else sys.stdout)


def _install(group: Component, *, repo: Path, scope: StartScope, host: Host) -> None:
    step = plan_install(group, repo=repo, macos=scope.macos)
    if not step.script.is_file():
        print(missing_script_line(step.script), file=sys.stderr)
        return
    print(step.announcement)
    try:
        result = host.run(list(step.argv), cwd=repo, timeout=HOST_PACKAGE_INSTALL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        print(launch_failure_line(step.script, str(exc)), file=sys.stderr)
        return
    if result.returncode != 0:
        print(exit_line(step.script, result.returncode), file=sys.stderr)


# meiva:note
# Install what is missing, read the host again, and refuse the start when anything remains.
# Returns 0 when every required tool is present, whether or not an install ran.
def ensure_host_dependencies(*, repo: Path, scope: StartScope, probing: Probing) -> int:
    first = plan_gate(
        missing_tools(scope, read_tools(scope, probing)), repo=repo, macos=scope.macos
    )
    match first:
        case Satisfied():
            return 0
        case Refuse(report, refusal):
            _emit(report)
            _emit(refusal, err=True)
            return 1
        case InstallGroups(report, groups):
            _emit(report)
            for group in groups:
                _install(group, repo=repo, scope=scope, host=probing.host)
        case _:
            assert_never(first)
    final = plan_recheck(
        missing_tools(scope, read_tools(scope, probing)), repo=repo, macos=scope.macos
    )
    match final:
        case Refuse(_, refusal):
            _emit(refusal, err=True)
            return 1
        case Ready(line):
            print(line)
            return 0
        case _:
            assert_never(final)


# meiva:note
# OS jobs live in one namespace per OS user, so both gates apply: the switch is on AND this is
# the default home (a test or worktree home owns neither).
def _os_jobs_owned(*, enabled: bool, roots: JobRoots) -> bool:
    return enabled and owns_os_job_home("host dependency preflight", roots=roots)


# meiva:note
# The selection this converge context describes, read once from its configuration and host.
def scope_for(ctx: ConvergeCtx) -> StartScope:
    view = ctx.read_config().view
    plane = data_plane_config_from_boot(ctx.config) if "gateway" in (ctx.roles or ()) else None
    return StartScope(
        roles=ctx.roles,
        services=ctx.services,
        data_plane=(
            None
            if plane is None
            else PlaneFacts(is_remote=plane.is_remote, pgbouncer_enabled=plane.pgbouncer_enabled)
        ),
        macos=ctx.host.platform.system == "darwin",
        is_root=ctx.host.uid == 0,
        os_jobs=_os_jobs_owned(enabled=view.general.os_jobs_enabled, roots=ctx.job_roots),
        permissions_helper=view.services.permissions_helper_enabled,
        require_github_pr=view.general.require_github_pr,
        memory_keep_local=view.general.memory_keep_local,
        prod_install=is_prod_install(repo=ctx.repo, home=ctx.meiva_home),
    )


# meiva:note
# Where the gate looks for this converge context: its host, its data-plane configuration
# and the process PATH extended with the provisioned Node locations.
def probing_for(ctx: ConvergeCtx) -> Probing:
    config = ctx.read_config()
    return Probing(
        host=ctx.host,
        data_plane=data_plane_config_from_boot(config) if "gateway" in (ctx.roles or ()) else None,
        toolchain_path=frontend_toolchain_path(config.process_environment().get("PATH", "")),
        home=ctx.meiva_home,
    )


def ensure_host_dependencies_step(ctx: ConvergeCtx) -> None:
    if ensure_host_dependencies(repo=ctx.repo, scope=scope_for(ctx), probing=probing_for(ctx)):
        raise RuntimeError(
            "host dependency preflight refused this start; install the tools named above "
            "and retry "
            "`meiva start --all-services` (or this unit's own service selection)"
        )
