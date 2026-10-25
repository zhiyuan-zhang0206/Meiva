# meiva:note
# The platform a start is given reaches the Redis command line: which binary runs and which
# addresses it binds.
#
# `redis_plan` decides the bind addresses as values (`test_redis_plan.py`); this file asserts that
# `redis_launch_argv`, the shell's wiring of the platform value into that decision, hands the
# command line of the platform it was given. The binaries are executable files in the test's own
# directory, found the way a configured `MEIVA_REDIS_BIN_DIR` is.

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.configured import ConfiguredMachineIdentity
from cli.commands.data_plane import cluster_instance as instance
from cli.commands.data_plane.config import DataPlaneConfig
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime

_REACHABLE = "10.9.8.7"


@pytest.fixture
def plane(data_plane_config: DataPlaneConfig, tmp_path: Path) -> DataPlaneConfig:
    bin_dir = tmp_path / "redis-bin"
    bin_dir.mkdir()
    for tool in ("redis-server", "redis-cli"):
        (bin_dir / tool).write_text("#!/bin/sh\n")
        (bin_dir / tool).chmod(0o755)
    return dataclasses.replace(data_plane_config, redis_bin_dir=str(bin_dir))


def _bound(argv: list[str]) -> list[str]:
    start = argv.index("--bind") + 1
    end = argv.index("--protected-mode")
    return argv[start:end]


def test_macos_binds_loopback_alone_even_when_the_machine_declares_an_address(
    plane: DataPlaneConfig, command_machine: ConfiguredMachineIdentity, tmp_path: Path
) -> None:
    command_machine.set(host=_REACHABLE)

    argv = instance.redis_launch_argv(plane, 6379, tmp_path, command_machine, macos=True)

    assert _bound(argv) == ["127.0.0.1"]


def test_another_platform_binds_the_declared_address_beside_loopback(
    plane: DataPlaneConfig, command_machine: ConfiguredMachineIdentity, tmp_path: Path
) -> None:
    command_machine.set(host=_REACHABLE)

    argv = instance.redis_launch_argv(plane, 6379, tmp_path, command_machine, macos=False)

    assert _bound(argv) == ["127.0.0.1", _REACHABLE]


def test_a_single_box_binds_loopback_on_either_platform(
    plane: DataPlaneConfig, command_machine: ConfiguredMachineIdentity, tmp_path: Path
) -> None:
    command_machine.set(host="localhost")
    for macos in (True, False):
        argv = instance.redis_launch_argv(plane, 6379, tmp_path, command_machine, macos=macos)

        assert _bound(argv) == ["127.0.0.1"]


def test_the_command_line_names_the_configured_server_port_and_data_directory(
    plane: DataPlaneConfig, command_machine: MachineIdentityReader, tmp_path: Path
) -> None:
    argv = instance.redis_launch_argv(plane, 6391, tmp_path, command_machine, macos=False)

    assert argv[0] == str(Path(plane.redis_bin_dir) / "redis-server")
    assert argv[argv.index("--port") + 1] == "6391"
    assert argv[argv.index("--dir") + 1] == str(tmp_path)
