from __future__ import annotations

from pathlib import Path

import pytest

from base.host.system.backend import (
    LinuxPlatformBackend,
    MacPlatformBackend,
    PlatformBackend,
    backend_for_system,
    get_backend,
)
from base.host.system.boot_autostart import registration as autostart
from base.host.system.boot_unit import BootUnitContext
from base.host.system.tests.fake_commands import LINUX_X86, FakeCommands, Reply, fake_host


def test_get_backend_returns_correct_type() -> None:
    backend = get_backend()
    assert isinstance(backend, PlatformBackend)


def test_unsupported_host_does_not_select_linux_jobs() -> None:
    assert isinstance(backend_for_system("darwin"), MacPlatformBackend)
    assert isinstance(backend_for_system("linux"), LinuxPlatformBackend)
    with pytest.raises(RuntimeError, match="unsupported host platform"):
        backend_for_system("freebsd")


def test_mac_backend_venv_bin_dir() -> None:
    backend = MacPlatformBackend()
    assert backend.venv_bin_dir_name() == "bin"


def test_linux_backend_venv_bin_dir() -> None:
    backend = LinuxPlatformBackend()
    assert backend.venv_bin_dir_name() == "bin"


def test_backend_type_is_stable_across_calls() -> None:
    assert type(get_backend()) is type(get_backend())


def test_linux_autostart_has_one_native_manager(tmp_path: Path) -> None:
    unit_dir = tmp_path / "etc-systemd"
    unit_dir.mkdir()
    runtime_dir = tmp_path / "run-systemd"
    runtime_dir.mkdir()
    ctx = BootUnitContext(
        home=tmp_path / "home",
        repo=tmp_path / "repo",
        user="meiva-user",
        group="meiva-group",
        home_dir=tmp_path / "user-home",
        services=frozenset({"gateway"}),
    )
    commands = FakeCommands()
    commands.on(["install", "-m", "0644"], Reply())
    commands.on(["systemctl", "daemon-reload"], Reply(), repeat=True)
    commands.on(["systemctl", "is-enabled"], Reply("disabled\n"))
    commands.on(["systemctl", "enable", "meiva-boot.service"], Reply())
    commands.on(["systemctl", "disable", "--now", "meiva-boot.service"], Reply())
    commands.on(["rm", "-f"], Reply())
    commands.on(["systemctl", "show"], Reply("inactive\n"))
    host = fake_host(
        commands,
        platform=LINUX_X86,
        uid=0,
        programs={"systemctl": "/usr/bin/systemctl"},
    )
    unit_file = unit_dir / "meiva-boot.service"
    assert (
        autostart.register_linux(
            services=frozenset({"gateway"}),
            host=host,
            context=ctx,
            unit_dir=unit_dir,
            runtime_dir=runtime_dir,
        )
        == 0
    )
    assert commands.argvs[0][:3] == ("install", "-m", "0644")
    assert commands.argvs[0][-1] == str(unit_file)
    unit_file.write_text("installed\n")
    assert (
        autostart.unregister_linux(
            host=host,
            unit_dir=unit_dir,
            runtime_dir=runtime_dir,
        )
        == 0
    )
    assert ("rm", "-f", str(unit_file)) in commands.argvs


def test_linux_autostart_propagates_unavailable_systemd() -> None:
    with pytest.raises(RuntimeError, match="systemd"):
        autostart.register_linux(
            services=frozenset({"gateway"}), host=fake_host(platform=LINUX_X86)
        )
