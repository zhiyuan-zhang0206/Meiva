# meiva:note
# What the host-dependency gate does with the tools it found missing.
#
# This is the first cold-start converge step, so a host that cannot satisfy the selected roster
# fails here, before converge touches the home and before any service launches, instead of dying
# halfway through either. A tool the repository can provision is installed by group and read
# again; a host-owned tool cannot be fixed by any provisioner, so its refusal is certain and is
# reported before minutes are spent installing the groups that could have been fixed. The gate
# shell prints the lines returned here and runs the installs.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from cli.commands.data_plane.host_dependencies.diagnosis import DependencyProblem
from cli.commands.data_plane.host_dependencies.table import Component, HostTool, provision_script

__all__ = [
    "GatePlan",
    "InstallGroups",
    "Missing",
    "Ready",
    "Refuse",
    "Satisfied",
    "plan_gate",
    "plan_recheck",
]

Missing = tuple[tuple[HostTool, DependencyProblem], ...]


# meiva:note
# Every required tool is present: the gate is silent.
@dataclass(frozen=True)
class Satisfied: ...


# meiva:note
# The start is refused. `report` goes to stdout and `refusal` to stderr, each line as printed
# (an empty line is a blank line).
@dataclass(frozen=True)
class Refuse:
    report: tuple[str, ...]
    refusal: tuple[str, ...]


# meiva:note
# Provision these groups in order, then read the host again. `report` is the stdout listing of
# what was found missing.
@dataclass(frozen=True)
class InstallGroups:
    report: tuple[str, ...]
    groups: tuple[Component, ...]


# meiva:note
# The groups installed and every required tool is now present.
@dataclass(frozen=True)
class Ready:
    line: str


GatePlan = Satisfied | Refuse | InstallGroups


def _groups(missing: Missing) -> tuple[Component, ...]:
    wanted: list[Component] = []
    for tool, problem in missing:
        if tool.component is None or not problem.installable:
            continue
        if tool.component not in wanted:
            wanted.append(tool.component)
    return tuple(wanted)


def _report(missing: Missing) -> tuple[str, ...]:
    return ("", "→ host dependencies", *(f"  · {tool.name}: {p.detail}" for tool, p in missing))


# meiva:note
# The Homebrew command that installs what `component` provisions on a Mac.
def _homebrew_install(component: Component) -> str:
    match component:
        case Component.DATA_PLANE:
            return "brew install postgresql@17 redis@8.2 pgbouncer pgvector"
        case Component.NODE:
            return "brew install node && npm install -g pnpm@11.9.0"
        case Component.HOST_UTILS:
            return "brew install curl"
        case Component.CLI_TOOLS:
            return "brew install git gh"
        case _:
            assert_never(component)


def _remediation(missing: Missing, *, repo: Path, macos: bool) -> tuple[str, ...]:
    lines: list[str] = []
    groups = _groups(missing)
    if macos:
        if groups:
            lines.append("macOS needs Homebrew first: https://brew.sh")
        lines.extend(_homebrew_install(component) for component in Component if component in groups)
    else:
        for group in groups:
            script = repo / "scripts" / "provision" / provision_script(group)
            lines.append(f"Debian/Ubuntu: sudo bash {script}")
        if groups:
            lines.append(
                "Other Linux: install the same packages with your distribution's package manager"
            )
            lines.append("No sudo on this host? run the provision scripts as root")
    for tool, problem in missing:
        if (tool.component is None or not problem.installable) and tool.repair:
            lines.append(tool.repair)
    return tuple(lines)


def _refusal(missing: Missing, *, repo: Path, macos: bool) -> tuple[str, ...]:
    lines = [
        "",
        "✗ host dependency preflight failed: `meiva start` launched nothing.",
        *(f"    ✗ {tool.name}: {problem.detail}" for tool, problem in missing),
    ]
    remediation = _remediation(missing, repo=repo, macos=macos)
    if remediation:
        lines.extend(("", "  Fix the host, then retry `meiva start`:"))
        lines.extend(f"    {line}" for line in remediation)
    return tuple(lines)


# meiva:note
# The first reading of the host.
#
# Nothing missing is `Satisfied`. A missing host-owned tool, or one whose problem no provisioner
# can fix (an unusable override), refuses at once. Otherwise the missing groups are installed.
def plan_gate(missing: Missing, *, repo: Path, macos: bool) -> GatePlan:
    if not missing:
        return Satisfied()
    report = _report(missing)
    if any(tool.component is None or not problem.installable for tool, problem in missing):
        return Refuse(report, _refusal(missing, repo=repo, macos=macos))
    return InstallGroups(report, _groups(missing))


# meiva:note
# The reading after the installs ran: whatever is still missing refuses, nothing is ready.
def plan_recheck(remaining: Missing, *, repo: Path, macos: bool) -> Ready | Refuse:
    if remaining:
        return Refuse((), _refusal(remaining, repo=repo, macos=macos))
    return Ready("  ✓ host dependencies ready")
