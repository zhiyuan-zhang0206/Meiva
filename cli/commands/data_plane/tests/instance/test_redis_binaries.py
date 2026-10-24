# meiva:note
# Which Redis tools the cluster runs: the choice is a value, the shell resolves it.
#
# The override directory, the pinned Homebrew formula and the bare PATH name are asserted on the
# returned source; the shell's resolution of an override is exercised against real files.

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from cli.commands.data_plane import cluster_instance as instance
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.instance.redis_binaries import (
    BREW_REDIS_FORMULA,
    BrewFormula,
    OnPath,
    OverrideDir,
    redis_binary_source,
)


def test_a_configured_directory_wins_on_every_platform() -> None:
    for macos in (True, False):
        assert redis_binary_source(redis_bin_dir="/opt/redis/bin", macos=macos) == OverrideDir(
            Path("/opt/redis/bin")
        )


def test_macos_resolves_the_pinned_versioned_formula() -> None:
    assert redis_binary_source(redis_bin_dir="", macos=True) == BrewFormula("redis@8.2")
    assert BREW_REDIS_FORMULA == "redis@8.2"


def test_linux_resolves_the_bare_names_through_path() -> None:
    assert redis_binary_source(redis_bin_dir="", macos=False) == OnPath()


def test_the_shell_returns_a_bare_name_without_an_override_off_macos(
    data_plane_config: DataPlaneConfig,
) -> None:
    config = dataclasses.replace(data_plane_config, redis_bin_dir="")
    assert instance.redis_server_bin(config.redis_bin_dir, macos=False) == "redis-server"
    assert instance.redis_cli_bin(config.redis_bin_dir, macos=False) == "redis-cli"


def _tools(directory: Path, *, executable: tuple[str, ...]) -> Path:
    directory.mkdir()
    for name in ("redis-server", "redis-cli"):
        tool = directory / name
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755 if name in executable else 0o644)
    return directory


def test_an_override_with_both_executables_resolves_inside_it(
    tmp_path: Path, data_plane_config: DataPlaneConfig
) -> None:
    selected = _tools(tmp_path / "bin", executable=("redis-server", "redis-cli"))
    config = dataclasses.replace(data_plane_config, redis_bin_dir=str(selected))
    assert instance.redis_server_bin(config.redis_bin_dir, macos=False) == str(
        selected / "redis-server"
    )
    assert instance.redis_cli_bin(config.redis_bin_dir, macos=True) == str(selected / "redis-cli")


@pytest.mark.parametrize("missing", ["redis-server", "redis-cli"])
def test_an_incomplete_override_never_falls_back_to_path(
    tmp_path: Path, data_plane_config: DataPlaneConfig, missing: str
) -> None:
    selected = _tools(tmp_path / "bin", executable=("redis-server", "redis-cli"))
    (selected / missing).unlink()
    config = dataclasses.replace(data_plane_config, redis_bin_dir=str(selected))
    for resolve in (instance.redis_server_bin, instance.redis_cli_bin):
        with pytest.raises(RuntimeError, match=missing):
            resolve(config.redis_bin_dir, macos=False)


def test_a_non_executable_override_tool_is_refused(
    tmp_path: Path, data_plane_config: DataPlaneConfig
) -> None:
    selected = _tools(tmp_path / "bin", executable=("redis-server",))
    config = dataclasses.replace(data_plane_config, redis_bin_dir=str(selected))
    with pytest.raises(RuntimeError, match="redis-cli"):
        instance.redis_server_bin(config.redis_bin_dir, macos=False)
