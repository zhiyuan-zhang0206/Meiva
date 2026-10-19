# meiva:note
# The external tools and OS services `meiva start` needs from its host.
#
# One table, one preflight. Every row names an executable that start's own steps
# (or the services it launches) invoke, the selection that makes it necessary, and
# what happens when it is absent: a group the repository can provision is
# installed through `scripts/provision/*` and re-probed; a host-owned tool refuses
# the start before converge with the exact repair. Nothing waits for the middle of
# converge to discover a missing binary — a host without `crontab` used to die at
# the WAL-G job step, long after the start had printed green steps above it.
#
# `render_table` prints the same rows, and
# `cli/commands/lifecycle/docs/start-host-dependencies.meiva.okf.md` carries its
# output, so the document cannot drift from what start enforces.
# meiva:link cli/commands/lifecycle/docs/start-host-dependencies.meiva.okf.md
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from base.cluster.dataplane.pg_tools import pg_tool
from base.deploy.progress_timeout import HOST_PACKAGE_INSTALL_TIMEOUT_S
from base.host.system.boot_unit import systemd_running
from base.host.system.hostport import Host
from base.paths.home import is_prod_install
from base.sessions.env_forwarding import frontend_toolchain_path
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.data_plane import cluster_instance as instance
from cli.commands.data_plane.config import DataPlaneConfig, data_plane_config_from_boot

DATA_PLANE = "data-plane"
NODE = "node"
HOST_UTILS = "host-utils"
CLI_TOOLS = "cli-tools"

_NODE_SERVICES = frozenset({"frontend", "browser", "browser-mcp"})

_PROVISION_SCRIPTS = {
    DATA_PLANE: "database.sh",
    NODE: "node.sh",
    HOST_UTILS: "host_utils.sh",
    CLI_TOOLS: "install-cli-tools.sh",
}

_PROVISION_LABELS = {
    DATA_PLANE: "PostgreSQL/Redis/PgBouncer",
    NODE: "Node.js",
    HOST_UTILS: "cron + curl",
    CLI_TOOLS: "git and the GitHub CLI",
}

# meiva:note
# The table's "who installs it" column.
_OWNERS = {
    DATA_PLANE: "`scripts/provision/database.sh` (apt/Homebrew)",
    NODE: "`scripts/provision/node.sh` (apt/Homebrew)",
    HOST_UTILS: "`scripts/provision/host_utils.sh` (apt; macOS ships both)",
    CLI_TOOLS: "`scripts/provision/install-cli-tools.sh` (apt/Homebrew)",
}
_HOST_OWNED = "the host; this repository cannot install it"


@dataclass(frozen=True)
class DependencyProblem:
    detail: str
    installable: bool = True


# meiva:note
# What this start selected and where it runs: the inputs every table row reads.
@dataclass(frozen=True)
class StartScope:
    roles: frozenset[str] | None
    services: frozenset[str]
    data_plane: DataPlaneConfig | None
    host: Host
    os_jobs: bool
    permissions_helper: bool
    # meiva:note
    # The memory pool consolidates through GitHub PRs on a split runner unless it
    # is kept local; these config fields decide whether `gh` is a start dependency.
    require_github_pr: bool
    memory_keep_local: bool
    # meiva:note
    # The boot unit is host-global: converge registers it only for the prod install
    # (default home and a checkout that is not a dev worktree).
    prod_install: bool

    @property
    def macos(self) -> bool:
        return self.host.platform.system == "darwin"

    @property
    def local_data_plane(self) -> bool:
        return (
            self.data_plane is not None
            and not self.data_plane.is_remote
            and bool(self.roles)
            and "gateway" in self.roles
        )


# meiva:note
# One external executable (or OS service) and the policy that covers its absence.
# `optional` rows are documentation only: start never blocks on them.
@dataclass(frozen=True)
class HostTool:
    name: str
    when: str
    component: str | None
    needed: Callable[[StartScope], bool]
    probe: Callable[[StartScope, HostDependencyProbes], DependencyProblem | None]
    repair: str = ""
    optional: bool = False

    def behavior(self) -> str:
        if self.optional:
            return "nothing: warn or skip, the start continues"
        if self.component is not None:
            return "install → re-probe; still missing → refuse"
        return "refuse before converge"


def _executable(binary: str | Path) -> bool:
    text = str(binary)
    if os.sep in text:
        path = Path(text)
        return path.is_file() and os.access(path, os.X_OK)
    return shutil.which(text) is not None


def _pg_probe(name: str) -> Callable[[StartScope, HostDependencyProbes], DependencyProblem | None]:
    def probe(_scope: StartScope, probes: HostDependencyProbes) -> DependencyProblem | None:
        try:
            path = probes.postgres(name)
        except RuntimeError:
            return DependencyProblem("not on PATH and not in the vendored PostgreSQL tree")
        return None if _executable(path) else DependencyProblem(f"{path} is not executable")

    return probe


def _redis_probe(
    kind: str,
) -> Callable[[StartScope, HostDependencyProbes], DependencyProblem | None]:
    def probe(scope: StartScope, probes: HostDependencyProbes) -> DependencyProblem | None:
        config = scope.data_plane
        if config is None:
            return DependencyProblem("no local data plane in this selection", installable=False)
        lookup = probes.redis_server if kind == "server" else probes.redis_cli
        try:
            binary = lookup(config)
        except RuntimeError as exc:
            # meiva:note
            # An override that points nowhere is not something apt can fix.
            return DependencyProblem(f"MEIVA_REDIS_BIN_DIR is unusable: {exc}", installable=False)
        return None if _executable(binary) else DependencyProblem(f"{binary} is not executable")

    return probe


def _pgbouncer_bin() -> str:
    from base.cluster.dataplane import pooler

    return pooler.pgbouncer_bin()


def _pgbouncer_probe(_scope: StartScope, probes: HostDependencyProbes) -> DependencyProblem | None:
    resolved = probes.pgbouncer()
    if _executable(resolved):
        return None
    # meiva:note
    # `pooler.pgbouncer_bin()` answers the bare name when nothing resolves, so a
    # separator is what tells "installed but unusable" apart from "not found".
    if os.sep in resolved:
        return DependencyProblem(f"{resolved!r} is not executable")
    return DependencyProblem("not on PATH (pgbouncer is not installed)")


def _which_probe(
    name: str,
) -> Callable[[StartScope, HostDependencyProbes], DependencyProblem | None]:
    def probe(scope: StartScope, _probes: HostDependencyProbes) -> DependencyProblem | None:
        return None if scope.host.which(name) is not None else DependencyProblem("not on PATH")

    return probe


def _shell_probe(_scope: StartScope, _probes: HostDependencyProbes) -> DependencyProblem | None:
    return None if Path("/bin/sh").is_file() else DependencyProblem("/bin/sh is missing")


def _systemd_probe(scope: StartScope, _probes: HostDependencyProbes) -> DependencyProblem | None:
    if systemd_running(host=scope.host):
        return None
    return DependencyProblem(
        "systemd is not this host's service manager (no systemctl, "
        "or /run/systemd/system is absent)"
    )


# meiva:note
# `which` with the frontend toolchain directory prepended: a Node installed by nvm
# or Homebrew may sit outside the PATH the CLI inherited.
def _tool_which(name: str) -> str | None:
    return shutil.which(name, path=frontend_toolchain_path(os.environ.get("PATH", "")))


def _node_tool_from_host(host: Host, name: str) -> DependencyProblem | None:
    return node_tool_problem(name, which=_tool_which, run=host.run)


# meiva:note
# Why one Node toolchain binary is unusable; `node` also carries the version floor
# the frontend declares (20.9, above Ubuntu 24.04's 18).
def node_tool_problem(
    name: str,
    *,
    which: Callable[[str], str | None] = _tool_which,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> DependencyProblem | None:
    resolved = which(name)
    if resolved is None:
        return DependencyProblem(f"{name} is not on PATH")
    if name != "node":
        return None
    try:
        # meiva:note
        # No `check=`: the injected runner is `Host.run` (`base.host.proc.run_bounded`),
        # whose timeout bounds the whole process tree and which deliberately does not
        # accept `check`. The caller reads `returncode` instead.
        result = run(
            [resolved, "-p", "process.versions.node"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return DependencyProblem(f"version probe failed: {exc}")
    raw = result.stdout.strip()
    version = _node_version(raw)
    if version is None or version < (20, 9):
        return DependencyProblem(f"{raw or 'version unknown'} is older than 20.9")
    return None


def _node_version(raw: str) -> tuple[int, int] | None:
    parts = raw.split(".")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


# meiva:note
# The binary lookups the rows use, injected so a test needs no host packages.
@dataclass(frozen=True)
class HostDependencyProbes:
    postgres: Callable[[str], Path] = pg_tool
    redis_server: Callable[[DataPlaneConfig], str] = instance.redis_server_bin
    redis_cli: Callable[[DataPlaneConfig], str] = instance.redis_cli_bin
    pgbouncer: Callable[[], str] = _pgbouncer_bin
    node: Callable[[Host, str], DependencyProblem | None] = _node_tool_from_host


def _always(_scope: StartScope) -> bool:
    return True


def _never(_scope: StartScope) -> bool:
    return False


def _needs_data_plane(scope: StartScope) -> bool:
    return scope.local_data_plane


def _needs_pgbouncer(scope: StartScope) -> bool:
    return (
        scope.local_data_plane
        and scope.data_plane is not None
        and scope.data_plane.pgbouncer_enabled
    )


def _needs_node(scope: StartScope) -> bool:
    return bool(scope.services & _NODE_SERVICES)


def _needs_os_jobs_linux(scope: StartScope) -> bool:
    return scope.os_jobs and not scope.macos


# meiva:note
# The boot unit is the one host-global registrar (`converge_host` skips it for a
# dev worktree), so systemd and the root step are required exactly where it runs.
def _needs_boot_unit(scope: StartScope) -> bool:
    return _needs_os_jobs_linux(scope) and scope.prod_install


def _needs_os_jobs_sudo(scope: StartScope) -> bool:
    return _needs_boot_unit(scope) and scope.host.uid != 0


def _needs_os_jobs_macos(scope: StartScope) -> bool:
    return scope.os_jobs and scope.macos


def _needs_frontend(scope: StartScope) -> bool:
    return "frontend" in scope.services


# meiva:note
# A split runner that consolidates memory through PRs cannot start without `gh`:
# `ensure_github_pr` refuses to converge there. A gateway consolidates locally, and
# a local-only pool never round-trips through GitHub at all.
def _needs_github_pr(scope: StartScope) -> bool:
    roles = scope.roles or frozenset()
    return (
        "gateway" not in roles
        and bool(roles)
        and scope.require_github_pr
        and not scope.memory_keep_local
    )


def _needs_macos_helper(scope: StartScope) -> bool:
    return scope.macos and scope.permissions_helper


# meiva:note
# The table. `when` is its "when start needs it" column; `repair` is printed only
# for a row the repository cannot install (a host-owned tool, or an operator
# override that points at a missing binary).
HOST_TOOLS: tuple[HostTool, ...] = (
    *(
        HostTool(
            name=name,
            when=when,
            component=DATA_PLANE,
            needed=_needs_data_plane,
            probe=_pg_probe(name),
        )
        for name, when in (
            ("initdb", "gateway with a local data plane: initializes the cluster's PostgreSQL 17"),
            ("pg_ctl", "gateway with a local data plane: starts and stops that instance"),
            ("postgres", "gateway with a local data plane: the server process itself"),
            (
                "pg_isready",
                "gateway with a local data plane: the readiness probe (a host client tool; "
                "the vendored tree omits it)",
            ),
        )
    ),
    HostTool(
        name="redis-server",
        when="gateway with a local data plane: the cluster's own Redis process",
        component=DATA_PLANE,
        needed=_needs_data_plane,
        probe=_redis_probe("server"),
        repair="fix MEIVA_REDIS_BIN_DIR: it must hold an executable redis-server and redis-cli",
    ),
    HostTool(
        name="redis-cli",
        when="gateway with a local data plane: authenticated admin and ACL commands",
        component=DATA_PLANE,
        needed=_needs_data_plane,
        probe=_redis_probe("cli"),
        repair="fix MEIVA_REDIS_BIN_DIR: it must hold an executable redis-server and redis-cli",
    ),
    HostTool(
        name="pgbouncer",
        when="gateway with a local data plane and pooling enabled: the connection pooler",
        component=DATA_PLANE,
        needed=_needs_pgbouncer,
        probe=_pgbouncer_probe,
    ),
    HostTool(
        name="node",
        when="frontend or browser service selected: builds and runs the Next.js app",
        component=NODE,
        needed=_needs_node,
        probe=lambda scope, probes: probes.node(scope.host, "node"),
    ),
    HostTool(
        name="npm",
        when="frontend or browser service selected: installs and builds the frontend",
        component=NODE,
        needed=_needs_node,
        probe=lambda scope, probes: probes.node(scope.host, "npm"),
    ),
    HostTool(
        name="npx",
        when="frontend or browser service selected: runs the frontend tooling and the "
        "meiva-browser daemon",
        component=NODE,
        needed=_needs_node,
        probe=lambda scope, probes: probes.node(scope.host, "npx"),
    ),
    HostTool(
        name="crontab",
        when="Linux, OS jobs enabled, default home: registers the daily backup, log rotation, "
        "packages refresh and health probe",
        component=HOST_UTILS,
        needed=_needs_os_jobs_linux,
        probe=_which_probe("crontab"),
    ),
    HostTool(
        name="systemctl",
        when="Linux prod install with OS jobs enabled: installs and enables the boot "
        "autostart unit (skipped for a dev worktree)",
        component=None,
        needed=_needs_boot_unit,
        probe=_systemd_probe,
        repair="install systemd and boot with it, or run `MEIVA_OS_JOBS_ENABLED=0 meiva start` "
        "to start without OS jobs",
    ),
    HostTool(
        name="sudo",
        when="Linux prod install with OS jobs enabled, not root: writes the boot unit as root",
        component=None,
        needed=_needs_os_jobs_sudo,
        probe=_which_probe("sudo"),
        repair="run `meiva start` as root, or allow passwordless sudo (`sudo -n true`) for the "
        "boot-unit install, or run `MEIVA_OS_JOBS_ENABLED=0 meiva start`",
    ),
    HostTool(
        name="launchctl",
        when="macOS, OS jobs enabled, default home: loads the launchd jobs",
        component=None,
        needed=_needs_os_jobs_macos,
        probe=_which_probe("launchctl"),
        repair="launchd ships with macOS; a host without it cannot register OS jobs — run "
        "`MEIVA_OS_JOBS_ENABLED=0 meiva start`",
    ),
    HostTool(
        name="curl",
        when="frontend service selected: the frontend readiness probe",
        component=HOST_UTILS,
        needed=_needs_frontend,
        probe=_which_probe("curl"),
    ),
    HostTool(
        name="swiftc",
        when="macOS with the permissions helper: compiles the helper app",
        component=None,
        needed=_needs_macos_helper,
        probe=_which_probe("swiftc"),
        repair="install the Apple Command Line Tools: `xcode-select --install`",
    ),
    HostTool(
        name="codesign",
        when="macOS with the permissions helper: signs the helper app",
        component=None,
        needed=_needs_macos_helper,
        probe=_which_probe("codesign"),
        repair="install the Apple Command Line Tools: `xcode-select --install`",
    ),
    HostTool(
        name="security",
        when="macOS with the permissions helper: the code-signing identity in the keychain",
        component=None,
        needed=_needs_macos_helper,
        probe=_which_probe("security"),
        repair="install the Apple Command Line Tools: `xcode-select --install`",
    ),
    HostTool(
        name="openssl",
        when="macOS with the permissions helper: key generation",
        component=None,
        needed=_needs_macos_helper,
        probe=_which_probe("openssl"),
        repair="install the Apple Command Line Tools: `xcode-select --install`",
    ),
    HostTool(
        name="sh",
        when="always: every supervised service and OS job line launches through /bin/sh",
        component=None,
        needed=_always,
        probe=_shell_probe,
        repair="a POSIX shell is part of every supported platform; repair /bin/sh before starting",
    ),
    # meiva:note
    # Optional rows: documentation, not gates — start warns or skips their step.
    HostTool(
        name="git",
        when="always: enumerates tracked migrations and digests the checkout on every start",
        component=CLI_TOOLS,
        needed=_always,
        probe=_which_probe("git"),
        repair="install git with your package manager: `sudo apt-get install -y git` "
        "(macOS: `xcode-select --install`)",
    ),
    HostTool(
        name="gh",
        when="split agent-runner that consolidates the memory pool through GitHub PRs "
        "(not a gateway, MEIVA_MEMORY_KEEP_LOCAL unset, MEIVA_REQUIRE_GITHUB_PR on)",
        component=CLI_TOOLS,
        needed=_needs_github_pr,
        probe=_which_probe("gh"),
        repair="install the GitHub CLI, then run `gh auth login` and grant the account write "
        "access to the memory repo; or set MEIVA_REQUIRE_GITHUB_PR=false / "
        "MEIVA_MEMORY_KEEP_LOCAL=1 to run without PR consolidation",
    ),
    HostTool(
        name="brew",
        when="never required: the macOS Homebrew pin drift check",
        component=None,
        needed=_never,
        probe=_which_probe("brew"),
        optional=True,
    ),
    HostTool(
        name="lsof",
        when="never required: fallback listener discovery when psutil cannot name a port holder",
        component=None,
        needed=_never,
        probe=_which_probe("lsof"),
        optional=True,
    ),
    HostTool(
        name="ps",
        when="never required: parent-chain inspection of launchd jobs (macOS always ships it)",
        component=None,
        needed=_never,
        probe=_which_probe("ps"),
        optional=True,
    ),
)


def required_tools(scope: StartScope) -> tuple[HostTool, ...]:
    return tuple(tool for tool in HOST_TOOLS if tool.needed(scope))


def missing_tools(
    scope: StartScope, *, probes: HostDependencyProbes | None = None
) -> tuple[tuple[HostTool, DependencyProblem], ...]:
    active = probes or HostDependencyProbes()
    found: list[tuple[HostTool, DependencyProblem]] = []
    for tool in required_tools(scope):
        problem = tool.probe(scope, active)
        if problem is not None:
            found.append((tool, problem))
    return tuple(found)


# meiva:note
# The dependency table, as the owner doc carries it; a test in the lifecycle suite
# fails when the rendered table and the document disagree.
def render_table() -> str:
    lines = [
        "| Tool | When `meiva start` needs it | Who installs it | Missing → start |",
        "|---|---|---|---|",
    ]
    for tool in HOST_TOOLS:
        owner = _OWNERS.get(tool.component or "", _HOST_OWNED)
        lines.append(f"| `{tool.name}` | {tool.when} | {owner} | {tool.behavior()} |")
    return "\n".join(lines)


# meiva:note
# Install what is missing, re-probe, and refuse the start when anything remains.
# This is the first cold-start converge step, so a host that cannot satisfy the
# selected roster fails here — before converge touches the home and before any
# service launches — instead of dying halfway through either. Returns 0 when every
# required tool is present, whether or not an install ran.
def ensure_host_dependencies(
    *,
    repo: Path,
    scope: StartScope,
    probes: HostDependencyProbes | None = None,
    install: Callable[[Path, str], None] | None = None,
) -> int:
    active = probes or HostDependencyProbes()
    installer = install or _install
    missing = missing_tools(scope, probes=active)
    if not missing:
        return 0
    _report_missing(missing)
    # meiva:note
    # A host-owned tool cannot be fixed by any provisioner, so its refusal is
    # certain: report it before spending minutes installing the groups that could
    # have been fixed.
    if any(tool.component is None or not problem.installable for tool, problem in missing):
        _report_refusal(missing, repo=repo, macos=scope.macos)
        return 1
    for component in _components(missing):
        installer(repo, component)
    remaining = missing_tools(scope, probes=active)
    if remaining:
        _report_refusal(remaining, repo=repo, macos=scope.macos)
        return 1
    print("  ✓ host dependencies ready")
    return 0


def ensure_host_dependencies_step(
    ctx: ConvergeCtx, *, check: Callable[..., int] = ensure_host_dependencies
) -> None:
    config = ctx.read_config()
    view = config.view
    data_plane = data_plane_config_from_boot(config) if "gateway" in (ctx.roles or ()) else None
    scope = StartScope(
        roles=ctx.roles,
        services=ctx.services,
        data_plane=data_plane,
        host=ctx.host,
        os_jobs=_os_jobs_owned(enabled=view.general.os_jobs_enabled),
        permissions_helper=view.services.permissions_helper_enabled,
        require_github_pr=view.general.require_github_pr,
        memory_keep_local=view.general.memory_keep_local,
        prod_install=is_prod_install(repo=ctx.repo, home=ctx.meiva_home),
    )
    if check(repo=ctx.repo, scope=scope):
        raise RuntimeError(
            "host dependency preflight refused this start; install the tools named above "
            "and retry `meiva start`"
        )


# meiva:note
# OS jobs live in one namespace per OS user, so both gates apply: the switch is on
# AND this is the default home (a test or worktree home owns neither).
def _os_jobs_owned(*, enabled: bool) -> bool:
    from base.host.system.cron import owns_os_jobs

    return enabled and owns_os_jobs("host dependency preflight")


def _components(missing: tuple[tuple[HostTool, DependencyProblem], ...]) -> tuple[str, ...]:
    wanted: list[str] = []
    for tool, problem in missing:
        if tool.component is None or not problem.installable:
            continue
        if tool.component not in wanted:
            wanted.append(tool.component)
    return tuple(wanted)


def _report_missing(missing: tuple[tuple[HostTool, DependencyProblem], ...]) -> None:
    print("\n→ host dependencies")
    for tool, problem in missing:
        print(f"  · {tool.name}: {problem.detail}")


def _report_refusal(
    missing: tuple[tuple[HostTool, DependencyProblem], ...], *, repo: Path, macos: bool
) -> None:
    print(
        "\n✗ host dependency preflight failed: `meiva start` launched nothing.",
        file=sys.stderr,
    )
    for tool, problem in missing:
        print(f"    ✗ {tool.name}: {problem.detail}", file=sys.stderr)
    lines = _remediation(missing, repo=repo, macos=macos)
    if lines:
        print("\n  Fix the host, then retry `meiva start`:", file=sys.stderr)
        for line in lines:
            print(f"    {line}", file=sys.stderr)


def _remediation(
    missing: tuple[tuple[HostTool, DependencyProblem], ...], *, repo: Path, macos: bool
) -> list[str]:
    lines: list[str] = []
    components = _components(missing)
    if macos:
        if components:
            lines.append("macOS needs Homebrew first: https://brew.sh")
        if DATA_PLANE in components:
            lines.append("brew install postgresql@17 redis@8.2 pgbouncer pgvector")
        if NODE in components:
            lines.append("brew install node")
        if HOST_UTILS in components:
            lines.append("brew install curl")
        if CLI_TOOLS in components:
            lines.append("brew install git gh")
    else:
        for component in components:
            script = repo / "scripts" / "provision" / _PROVISION_SCRIPTS[component]
            lines.append(f"Debian/Ubuntu: sudo bash {script}")
        if components:
            lines.append(
                "Other Linux: install the same packages with your distribution's package manager"
            )
            lines.append("No sudo on this host? run the provision scripts as root")
    for tool, problem in missing:
        if (tool.component is None or not problem.installable) and tool.repair:
            lines.append(tool.repair)
    return lines


def _install(repo: Path, component: str) -> None:
    script = repo / "scripts" / "provision" / _PROVISION_SCRIPTS[component]
    if not script.is_file():
        print(f"  ✗ provision script missing: {script}", file=sys.stderr)
        return
    print(
        f"  · installing {_PROVISION_LABELS[component]} with {_package_manager()} "
        "(this can take several minutes)"
    )
    try:
        result = subprocess.run(
            ["bash", str(script)],
            cwd=repo,
            check=False,
            timeout=HOST_PACKAGE_INSTALL_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  ✗ {script.name} failed to run: {exc}", file=sys.stderr)
        return
    if result.returncode != 0:
        print(
            f"  ! {script.name} exited {result.returncode}; re-checking what landed",
            file=sys.stderr,
        )


def _package_manager() -> str:
    from base.native_process.os_platform import is_macos

    return "Homebrew" if is_macos() else "apt"
