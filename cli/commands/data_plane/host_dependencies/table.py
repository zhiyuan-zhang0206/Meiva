# meiva:note
# The external tools and OS services `meiva start` needs from its host, as one table.
#
# Every row names an executable that start's own steps (or the services it launches) invoke,
# the selection that makes it necessary (`Need`), how it is read (`Probe`) and what happens when
# it is absent: a group the repository can provision is installed through `scripts/provision/*`
# and read again; a host-owned tool refuses the start before converge with the exact repair.
# Nothing waits for the middle of converge to discover a missing binary: a host without
# `crontab` used to die at the WAL-G job step, long after the start had printed green steps
# above it.
#
# `render_table` prints the same rows, and
# `cli/commands/lifecycle/docs/start-host-dependencies.meiva.okf.md` carries its output, so the
# document cannot drift from what start enforces.
# meiva:link cli/commands/lifecycle/docs/start-host-dependencies.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import assert_never

from cli.commands.data_plane.host_dependencies.scope import StartScope

__all__ = [
    "HOST_TOOLS",
    "Component",
    "HostTool",
    "Need",
    "Probe",
    "provision_label",
    "provision_script",
    "render_table",
    "required_tools",
]

_HOST_OWNED = "the host; this repository cannot install it"


# meiva:note
# A group of tools one provisioning script installs together.
#
# The value is the group's name in the owner document
# (`cli/commands/lifecycle/docs/start-host-dependencies.meiva.okf.md`); a tool that no script
# installs has no component, and a refusal names its repair instead.
class Component(StrEnum):
    DATA_PLANE = "data-plane"
    NODE = "node"
    HOST_UTILS = "host-utils"
    CLI_TOOLS = "cli-tools"


# meiva:note
# The selection that makes a row necessary.
class Need(Enum):
    ALWAYS = "always"
    NEVER = "never"
    DATA_PLANE = "data plane"
    POOLER = "pooler"
    NODE = "node"
    OS_JOBS_LINUX = "os jobs on linux"
    BOOT_UNIT = "boot unit"
    OS_JOBS_SUDO = "os jobs through sudo"
    OS_JOBS_MACOS = "os jobs on macos"
    FRONTEND = "frontend"
    GITHUB_PR = "github pr"
    MACOS_HELPER = "macos helper"


# meiva:note
# How a row's tool is read from the host.
class Probe(Enum):
    PG_TOOL = "pg tool"
    REDIS_SERVER = "redis-server"
    REDIS_CLI = "redis-cli"
    POOLER = "pgbouncer"
    NODE_TOOL = "node toolchain"
    WHICH = "program on PATH"
    SHELL = "/bin/sh"
    SYSTEMD = "systemd"


# meiva:note
# One external executable (or OS service) and the policy that covers its absence.
#
# `optional` rows are documentation only: start never blocks on them.
@dataclass(frozen=True)
class HostTool:
    name: str
    when: str
    component: Component | None
    need: Need
    probe: Probe
    repair: str = ""
    optional: bool = False

    # meiva:note
    # What start does when the tool is missing, as the table's last column prints it.
    def behavior(self) -> str:
        if self.optional:
            return "nothing: warn or skip, the start continues"
        if self.component is not None:
            return "install → re-probe; still missing → refuse"
        return "refuse before converge"


# meiva:note
# The provisioning script a group's missing tool is installed through, relative to
# `scripts/provision`.
def provision_script(component: Component) -> str:
    match component:
        case Component.DATA_PLANE:
            return "database.sh"
        case Component.NODE:
            return "node.sh"
        case Component.HOST_UTILS:
            return "host_utils.sh"
        case Component.CLI_TOOLS:
            return "install-cli-tools.sh"
        case _:
            assert_never(component)


# meiva:note
# What a provisioning group installs, as the install announcement words it.
def provision_label(component: Component) -> str:
    match component:
        case Component.DATA_PLANE:
            return "PostgreSQL/Redis/PgBouncer"
        case Component.NODE:
            return "Node.js"
        case Component.HOST_UTILS:
            return "cron + curl"
        case Component.CLI_TOOLS:
            return "git, the GitHub CLI and the other CLI tools"
        case _:
            assert_never(component)


# meiva:note
# Who installs a tool of `component`: the group's script, or the host when no script does.
def _owner(component: Component | None) -> str:
    match component:
        case None:
            return _HOST_OWNED
        case Component.DATA_PLANE:
            return "`scripts/provision/database.sh` (apt/Homebrew)"
        case Component.NODE:
            return "`scripts/provision/node.sh` (apt/Homebrew)"
        case Component.HOST_UTILS:
            return "`scripts/provision/host_utils.sh` (apt; macOS ships both)"
        case Component.CLI_TOOLS:
            return "`scripts/provision/install-cli-tools.sh` (apt/Homebrew)"
        case _:
            assert_never(component)


def _needed(need: Need, scope: StartScope) -> bool:
    match need:
        case Need.ALWAYS:
            return True
        case Need.NEVER:
            return False
        case Need.DATA_PLANE:
            return scope.local_data_plane
        case Need.POOLER:
            return scope.pooled
        case Need.NODE:
            return scope.node_selected
        case Need.OS_JOBS_LINUX:
            return scope.linux_os_jobs
        case Need.BOOT_UNIT:
            return scope.boot_unit
        case Need.OS_JOBS_SUDO:
            return scope.boot_unit_through_sudo
        case Need.OS_JOBS_MACOS:
            return scope.macos_os_jobs
        case Need.FRONTEND:
            return "frontend" in scope.services
        case Need.GITHUB_PR:
            return scope.github_pr_consolidation
        case Need.MACOS_HELPER:
            return scope.macos_helper
        case _:
            assert_never(need)


# meiva:note
# The table. `when` is its "when start needs it" column; `repair` is printed only for a row the
# repository cannot install (a host-owned tool, or an operator override that points at a
# missing binary). The last three rows are optional: documentation, not gates.
HOST_TOOLS: tuple[HostTool, ...] = (
    HostTool(
        name="initdb",
        when="gateway with a local data plane: initializes the cluster's PostgreSQL 17",
        component=Component.DATA_PLANE,
        need=Need.DATA_PLANE,
        probe=Probe.PG_TOOL,
    ),
    HostTool(
        name="pg_ctl",
        when="gateway with a local data plane: starts and stops that instance",
        component=Component.DATA_PLANE,
        need=Need.DATA_PLANE,
        probe=Probe.PG_TOOL,
    ),
    HostTool(
        name="postgres",
        when="gateway with a local data plane: the server process itself",
        component=Component.DATA_PLANE,
        need=Need.DATA_PLANE,
        probe=Probe.PG_TOOL,
    ),
    HostTool(
        name="pg_isready",
        when="gateway with a local data plane: the readiness probe (a host client tool; "
        "the vendored tree omits it)",
        component=Component.DATA_PLANE,
        need=Need.DATA_PLANE,
        probe=Probe.PG_TOOL,
    ),
    HostTool(
        name="redis-server",
        when="gateway with a local data plane: the cluster's own Redis process",
        component=Component.DATA_PLANE,
        need=Need.DATA_PLANE,
        probe=Probe.REDIS_SERVER,
        repair="fix MEIVA_REDIS_BIN_DIR: it must hold an executable redis-server and redis-cli",
    ),
    HostTool(
        name="redis-cli",
        when="gateway with a local data plane: authenticated admin and ACL commands",
        component=Component.DATA_PLANE,
        need=Need.DATA_PLANE,
        probe=Probe.REDIS_CLI,
        repair="fix MEIVA_REDIS_BIN_DIR: it must hold an executable redis-server and redis-cli",
    ),
    HostTool(
        name="pgbouncer",
        when="gateway with a local data plane and pooling enabled: the connection pooler",
        component=Component.DATA_PLANE,
        need=Need.POOLER,
        probe=Probe.POOLER,
    ),
    HostTool(
        name="node",
        when="frontend or browser service selected: builds and runs the Next.js app",
        component=Component.NODE,
        need=Need.NODE,
        probe=Probe.NODE_TOOL,
    ),
    HostTool(
        name="pnpm",
        when="frontend service selected: installs, builds and runs the frontend",
        component=Component.NODE,
        need=Need.FRONTEND,
        probe=Probe.NODE_TOOL,
    ),
    HostTool(
        name="npm",
        when="frontend or browser service selected: ships with Node and installs pnpm",
        component=Component.NODE,
        need=Need.NODE,
        probe=Probe.NODE_TOOL,
    ),
    HostTool(
        name="npx",
        when="frontend or browser service selected: runs the meiva-browser MCP daemon",
        component=Component.NODE,
        need=Need.NODE,
        probe=Probe.NODE_TOOL,
    ),
    HostTool(
        name="crontab",
        when="Linux, OS jobs enabled, default home: registers the daily backup, log rotation, "
        "packages refresh and health probe",
        component=Component.HOST_UTILS,
        need=Need.OS_JOBS_LINUX,
        probe=Probe.WHICH,
    ),
    HostTool(
        name="systemctl",
        when="Linux prod install with OS jobs enabled: installs and enables the boot "
        "autostart unit (skipped for a dev worktree)",
        component=None,
        need=Need.BOOT_UNIT,
        probe=Probe.SYSTEMD,
        repair="install systemd and boot with it, or run `MEIVA_OS_JOBS_ENABLED=0 meiva start` "
        "to start without OS jobs",
    ),
    HostTool(
        name="sudo",
        when="Linux prod install with OS jobs enabled, not root: writes the boot unit as root",
        component=None,
        need=Need.OS_JOBS_SUDO,
        probe=Probe.WHICH,
        repair="run `meiva start --all-services` (or this unit's own service selection) as root, or allow passwordless sudo (`sudo -n true`) for the "
        "boot-unit install, or run `MEIVA_OS_JOBS_ENABLED=0 meiva start --all-services`",
    ),
    HostTool(
        name="launchctl",
        when="macOS, OS jobs enabled, default home: loads the launchd jobs",
        component=None,
        need=Need.OS_JOBS_MACOS,
        probe=Probe.WHICH,
        repair="launchd ships with macOS; a host without it cannot register OS jobs — run "
        "`MEIVA_OS_JOBS_ENABLED=0 meiva start --all-services`",
    ),
    HostTool(
        name="curl",
        when="frontend service selected: the frontend readiness probe",
        component=Component.HOST_UTILS,
        need=Need.FRONTEND,
        probe=Probe.WHICH,
    ),
    HostTool(
        name="swiftc",
        when="macOS with the permissions helper: compiles the helper app",
        component=None,
        need=Need.MACOS_HELPER,
        probe=Probe.WHICH,
        repair="install the Apple Command Line Tools: `xcode-select --install`",
    ),
    HostTool(
        name="codesign",
        when="macOS with the permissions helper: signs the helper app",
        component=None,
        need=Need.MACOS_HELPER,
        probe=Probe.WHICH,
        repair="install the Apple Command Line Tools: `xcode-select --install`",
    ),
    HostTool(
        name="security",
        when="macOS with the permissions helper: unlocks the keychain that holds the certificate it is signed with",
        component=None,
        need=Need.MACOS_HELPER,
        probe=Probe.WHICH,
        repair="`security` ships with macOS; repair /usr/bin/security before starting",
    ),
    HostTool(
        name="openssl",
        when="macOS with the permissions helper, first build on the host: creates that certificate",
        component=None,
        need=Need.MACOS_HELPER,
        probe=Probe.WHICH,
        repair="`openssl` ships with macOS at /usr/bin/openssl; repair it before starting",
    ),
    HostTool(
        name="sh",
        when="always: every supervised service and OS job line launches through /bin/sh",
        component=None,
        need=Need.ALWAYS,
        probe=Probe.SHELL,
        repair="a POSIX shell is part of every supported platform; repair /bin/sh before starting",
    ),
    HostTool(
        name="git",
        when="always: enumerates tracked migrations and digests the checkout on every start",
        component=Component.CLI_TOOLS,
        need=Need.ALWAYS,
        probe=Probe.WHICH,
        repair="install git with your package manager: `sudo apt-get install -y git` "
        "(macOS: `xcode-select --install`)",
    ),
    HostTool(
        name="gh",
        when="split agent-runner that consolidates the memory pool through GitHub PRs "
        "(not a gateway, MEIVA_MEMORY_KEEP_LOCAL unset, MEIVA_REQUIRE_GITHUB_PR on)",
        component=Component.CLI_TOOLS,
        need=Need.GITHUB_PR,
        probe=Probe.WHICH,
        repair="install the GitHub CLI, then run `gh auth login` and grant the account write "
        "access to the memory repo; or set MEIVA_REQUIRE_GITHUB_PR=false / "
        "MEIVA_MEMORY_KEEP_LOCAL=1 to run without PR consolidation",
    ),
    HostTool(
        name="brew",
        when="never required: the macOS Homebrew pin drift check",
        component=None,
        need=Need.NEVER,
        probe=Probe.WHICH,
        optional=True,
    ),
    HostTool(
        name="lsof",
        when="never required: fallback listener discovery when psutil cannot name a port holder",
        component=None,
        need=Need.NEVER,
        probe=Probe.WHICH,
        optional=True,
    ),
    HostTool(
        name="ps",
        when="never required: parent-chain inspection of launchd jobs (macOS always ships it)",
        component=None,
        need=Need.NEVER,
        probe=Probe.WHICH,
        optional=True,
    ),
)


# meiva:note
# The rows this selection requires, in table order.
def required_tools(scope: StartScope) -> tuple[HostTool, ...]:
    return tuple(tool for tool in HOST_TOOLS if _needed(tool.need, scope))


# meiva:note
# The dependency table, as the owner doc carries it; a test in the lifecycle suite fails when
# the rendered table and the document disagree.
def render_table() -> str:
    lines = [
        "| Tool | When `meiva start` needs it | Who installs it | Missing → start |",
        "|---|---|---|---|",
    ]
    for tool in HOST_TOOLS:
        lines.append(
            f"| `{tool.name}` | {tool.when} | {_owner(tool.component)} | {tool.behavior()} |"
        )
    return "\n".join(lines)
