# meiva:note
# What a reading of the host means for each required tool.
#
# The shell (`readings.py`) looks at the host once per required row and records what it saw as a
# `Reading`; this module turns a reading into the row's problem, or into none. A problem is data:
# the gate decides from it whether to install a group, refuse, or continue.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import assert_never

from cli.commands.data_plane.host_dependencies.scope import StartScope
from cli.commands.data_plane.host_dependencies.table import HostTool, Probe, required_tools

__all__ = [
    "Absent",
    "DependencyProblem",
    "Found",
    "NoLocalPlane",
    "NodeVersion",
    "ProbeFailed",
    "Reading",
    "Unusable",
    "diagnose",
    "missing_tools",
]

# meiva:note
# The oldest Node the frontend runs on (above Ubuntu 24.04's packaged 18).
_NODE_FLOOR = (20, 9)


# meiva:note
# Why a required tool is not usable, and whether a provisioning script could fix it.
@dataclass(frozen=True)
class DependencyProblem:
    detail: str
    installable: bool = True


# meiva:note
# The tool resolved to `path`; `executable` is whether the host can run that path.
@dataclass(frozen=True)
class Found:
    path: str
    executable: bool


# meiva:note
# Nothing named the tool: not on PATH, not in a vendored tree, not a present service.
@dataclass(frozen=True)
class Absent: ...


# meiva:note
# The resolver refused: an operator override that points nowhere, with its message.
@dataclass(frozen=True)
class Unusable:
    reason: str


# meiva:note
# `node` ran and printed this version (`process.versions.node`).
@dataclass(frozen=True)
class NodeVersion:
    raw: str


# meiva:note
# `node` was found but its version could not be read.
@dataclass(frozen=True)
class ProbeFailed:
    reason: str


# meiva:note
# The selection has no data plane to resolve a Redis binary for.
@dataclass(frozen=True)
class NoLocalPlane: ...


Reading = Found | Absent | Unusable | NodeVersion | ProbeFailed | NoLocalPlane


def _version(raw: str) -> tuple[int, int] | None:
    parts = raw.split(".")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _not_executable(found: Found) -> DependencyProblem | None:
    return None if found.executable else DependencyProblem(f"{found.path} is not executable")


def _node_problem(name: str, reading: Reading) -> DependencyProblem | None:
    match reading:
        case Absent():
            return DependencyProblem(f"{name} is not on PATH")
        case Found():
            return None
        case ProbeFailed(reason):
            return DependencyProblem(f"version probe failed: {reason}")
        case NodeVersion(raw):
            version = _version(raw)
            if version is None or version < _NODE_FLOOR:
                return DependencyProblem(f"{raw or 'version unknown'} is older than 20.9")
            return None
        case _:
            raise ValueError(f"{name}: {reading!r} is not a Node toolchain reading")


def _redis_problem(reading: Reading) -> DependencyProblem | None:
    match reading:
        case NoLocalPlane():
            return DependencyProblem("no local data plane in this selection", installable=False)
        case Unusable(reason):
            return DependencyProblem(
                f"MEIVA_REDIS_BIN_DIR is unusable: {reason}", installable=False
            )
        case Found():
            return _not_executable(reading)
        case _:
            raise ValueError(f"{reading!r} is not a Redis binary reading")


def _pooler_problem(reading: Reading) -> DependencyProblem | None:
    match reading:
        case Found(path, executable):
            if executable:
                return None
            # meiva:note
            # The resolver answers the bare name when nothing resolves, so a separator is what
            # tells "installed but unusable" apart from "not found".
            if "/" in path:
                return DependencyProblem(f"{path!r} is not executable")
            return DependencyProblem("not on PATH (pgbouncer is not installed)")
        case _:
            raise ValueError(f"{reading!r} is not a PgBouncer reading")


def _pg_problem(reading: Reading) -> DependencyProblem | None:
    match reading:
        case Absent():
            return DependencyProblem("not on PATH and not in the vendored PostgreSQL tree")
        case Found():
            return _not_executable(reading)
        case _:
            raise ValueError(f"{reading!r} is not a PostgreSQL tool reading")


def _presence_problem(probe: Probe, reading: Reading) -> DependencyProblem | None:
    match reading:
        case Found():
            return None
        case Absent():
            match probe:
                case Probe.SHELL:
                    return DependencyProblem("/bin/sh is missing")
                case Probe.SYSTEMD:
                    return DependencyProblem(
                        "systemd is not this host's service manager (no systemctl, "
                        "or /run/systemd/system is absent)"
                    )
                case Probe.WHICH:
                    return DependencyProblem("not on PATH")
                case _:
                    raise ValueError(f"{probe!r} is not a presence probe")
        case _:
            raise ValueError(f"{reading!r} is not a presence reading")


# meiva:note
# The problem a reading shows for `tool`, or None when the tool is usable.
#
# A reading of a kind the row's probe never produces is a defect in the shell, not a host
# condition, so it raises.
def diagnose(tool: HostTool, reading: Reading) -> DependencyProblem | None:
    match tool.probe:
        case Probe.PG_TOOL:
            return _pg_problem(reading)
        case Probe.REDIS_SERVER | Probe.REDIS_CLI:
            return _redis_problem(reading)
        case Probe.POOLER:
            return _pooler_problem(reading)
        case Probe.NODE_TOOL:
            return _node_problem(tool.name, reading)
        case Probe.WHICH | Probe.SHELL | Probe.SYSTEMD:
            return _presence_problem(tool.probe, reading)
        case _:
            assert_never(tool.probe)


# meiva:note
# The required tools this selection is missing, with why, in table order.
#
# `readings` holds one reading per required tool by name; a required tool without one raises
# `KeyError`, since a gate that skipped a row would pass a host it never looked at.
def missing_tools(
    scope: StartScope, readings: Mapping[str, Reading]
) -> tuple[tuple[HostTool, DependencyProblem], ...]:
    found: list[tuple[HostTool, DependencyProblem]] = []
    for tool in required_tools(scope):
        problem = diagnose(tool, readings[tool.name])
        if problem is not None:
            found.append((tool, problem))
    return tuple(found)
