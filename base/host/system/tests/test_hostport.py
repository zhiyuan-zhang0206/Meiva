from __future__ import annotations

import dataclasses
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import cast

import pytest

from base.host import proc
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import FakeCommands, Reply
from base.native_process.os_platform import Platform


def test_the_system_host_is_this_machine(tmp_path: Path) -> None:
    handed = {"DISPLAY": str(tmp_path / "x11"), "ONLY_IN_THE_HANDED_MAPPING": "1"}
    host = Host.system(environ=handed)
    assert dict(host.environ) == handed
    assert "ONLY_IN_THE_HANDED_MAPPING" not in os.environ
    assert host.run is proc.run_bounded
    assert host.which is shutil.which
    assert host.uid == os.getuid()
    assert host.platform.executable == sys.executable
    assert host.platform.machine == platform.machine()
    assert host.platform.system == ("darwin" if sys.platform == "darwin" else "linux")


def test_the_system_host_runs_a_real_command() -> None:
    result = Host.system(environ={}).run(["echo", "hi"], timeout=60, capture_output=True, text=True)
    assert (result.returncode, result.stdout) == (0, "hi\n")


def test_the_system_host_bounds_a_command_by_its_timeout() -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        Host.system(environ={}).run(["sleep", "30"], timeout=0.5)


def test_a_host_is_immutable_and_has_exactly_the_five_reviewed_fields() -> None:
    host = Host(
        run=FakeCommands(),
        which=lambda _name: None,
        platform=Platform("linux", "x86_64", "/usr/bin/python"),
        uid=1000,
        environ={},
    )
    assert [f.name for f in dataclasses.fields(Host)] == [
        "run",
        "which",
        "platform",
        "uid",
        "environ",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        host.__setattr__("uid", 1)


def test_a_host_environment_is_a_read_only_snapshot() -> None:
    handed = {"DISPLAY": ":0"}
    host = Host(
        run=FakeCommands(),
        which=lambda _name: None,
        platform=Platform("linux", "x86_64", "/usr/bin/python"),
        uid=1000,
        environ=handed,
    )
    handed["DISPLAY"] = ":1"
    handed["WAYLAND_DISPLAY"] = "wayland-0"
    assert dict(host.environ) == {"DISPLAY": ":0"}
    with pytest.raises(TypeError):
        cast("MutableMapping[str, str]", host.environ)["DISPLAY"] = ":2"


def test_a_test_builds_the_host_it_wants() -> None:
    commands = FakeCommands()
    commands.on("lsof", Reply(stdout="p1109\n"))
    host = Host(
        run=commands,
        which=lambda name: "/opt/bin/lsof" if name == "lsof" else None,
        platform=Platform("darwin", "arm64", "/usr/bin/python"),
        uid=501,
        environ={},
    )
    assert host.which("lsof") == "/opt/bin/lsof"
    assert host.which("nothing") is None
    done = host.run(["lsof", "-nP"], timeout=10, capture_output=True, text=True)
    assert done.stdout == "p1109\n"
    assert commands.argvs == [("lsof", "-nP")]
