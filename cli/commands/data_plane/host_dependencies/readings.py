# meiva:note
# Looks at the host for each required tool and records what it saw as a `Reading`.
#
# The shell half of the dependency gate. Programs are found through `Host.which`, commands run
# through `Host.run`, and the vendored PostgreSQL tree, the Redis override and the pooler binary
# through their own resolvers; `diagnosis.py` decides what each reading means.

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from base.cluster.dataplane import pooler
from base.cluster.dataplane.pg_tools import pg_tool
from base.host.system.boot_unit import systemd_running
from base.host.system.hostport import Host
from cli.commands.data_plane import cluster_instance as instance
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.host_dependencies.diagnosis import (
    Absent,
    Found,
    NodeVersion,
    NoLocalPlane,
    ProbeFailed,
    Reading,
    Unusable,
)
from cli.commands.data_plane.host_dependencies.scope import StartScope
from cli.commands.data_plane.host_dependencies.table import HostTool, Probe, required_tools

__all__ = ["Probing", "read_tools"]

_NODE_VERSION_TIMEOUT_S = 10


# meiva:note
# Where the gate looks: the host it runs commands and finds programs through, the data-plane
# configuration the Redis override comes from (None off a gateway), and the directories Node's
# toolchain is searched in.
#
# `toolchain_path` is the `PATH`-style string already extended with the provisioned Node
# locations: a Node installed by nvm or Homebrew may sit outside the PATH the CLI inherited.
@dataclass(frozen=True)
class Probing:
    host: Host
    data_plane: DataPlaneConfig | None
    toolchain_path: str


def _found(host: Host, path: str) -> Found:
    return Found(path, executable=host.which(path) is not None)


def _on_toolchain(probing: Probing, name: str) -> str | None:
    for directory in probing.toolchain_path.split(os.pathsep):
        if directory and (found := probing.host.which(f"{directory}/{name}")) is not None:
            return found
    return None


def _node_reading(probing: Probing, name: str) -> Reading:
    resolved = _on_toolchain(probing, name)
    if resolved is None:
        return Absent()
    if name != "node":
        return Found(resolved, executable=True)
    try:
        # meiva:note
        # No `check=`: `Host.run` is `base.host.proc.run_bounded`, whose timeout bounds the whole
        # process tree and which deliberately does not accept `check`. The result's stdout is
        # read instead.
        result = probing.host.run(
            [resolved, "-p", "process.versions.node"],
            capture_output=True,
            text=True,
            timeout=_NODE_VERSION_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ProbeFailed(str(exc))
    return NodeVersion(result.stdout.strip())


def _redis_reading(probing: Probing, scope: StartScope, probe: Probe) -> Reading:
    config = probing.data_plane
    if config is None:
        return NoLocalPlane()
    resolve = instance.redis_server_bin if probe == Probe.REDIS_SERVER else instance.redis_cli_bin
    try:
        binary = resolve(config.redis_bin_dir, macos=scope.macos)
    except RuntimeError as exc:
        return Unusable(str(exc))
    return _found(probing.host, binary)


def _read(tool: HostTool, scope: StartScope, probing: Probing) -> Reading:
    host = probing.host
    match tool.probe:
        case Probe.PG_TOOL:
            try:
                return _found(host, str(pg_tool(tool.name)))
            except RuntimeError:
                return Absent()
        case Probe.REDIS_SERVER | Probe.REDIS_CLI:
            return _redis_reading(probing, scope, tool.probe)
        case Probe.POOLER:
            return _found(host, pooler.pgbouncer_bin())
        case Probe.NODE_TOOL:
            return _node_reading(probing, tool.name)
        case Probe.WHICH:
            found = host.which(tool.name)
            return Absent() if found is None else Found(found, executable=True)
        case Probe.SHELL:
            return Found("/bin/sh", executable=True) if Path("/bin/sh").is_file() else Absent()
        case Probe.SYSTEMD:
            return Found("systemd", executable=True) if systemd_running(host=host) else Absent()
        case _:
            assert_never(tool.probe)


# meiva:note
# One reading per tool this selection requires, by tool name.
def read_tools(scope: StartScope, probing: Probing) -> dict[str, Reading]:
    return {tool.name: _read(tool, scope, probing) for tool in required_tools(scope)}
