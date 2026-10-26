from collections.abc import Callable
from pathlib import Path

import pytest

from base.host.proc import CommandRunner
from base.host.system import logs_job, packages_job, pr_flow_job, walg_job
from base.host.system.boot_autostart import registration as autostart
from base.host.system.cron import health_job
from base.host.system.job_values import JobRoots
from base.host.system.packages_content import RefreshSchedule
from base.host.system.tests.fake_commands import (
    DARWIN_ARM64,
    LINUX_X86,
    FakeCommands,
    Reply,
    fake_host,
)
from base.host.system.tests.fake_crontab import FakeCrontab


@pytest.mark.parametrize("enabled", [False, True])
def test_packages_gates_stop_at_the_switch_then_the_home(tmp_path: Path, enabled: bool) -> None:
    packages_job.register_packages_job(
        roots=JobRoots(home=tmp_path / "scratch-home", repo=Path("/isolated"), user_home=tmp_path),
        enabled=enabled,
        schedule=None,
        host=fake_host(),
    )
    # meiva:note
    # A scratch home must stop before the deliberately absent schedule or platform.


def test_packages_refresh_gate_follows_home_and_precedes_platform(
    default_home: Path,
) -> None:
    commands = FakeCommands()
    packages_job.register_packages_job(
        roots=JobRoots(
            home=default_home / ".meiva", repo=Path("/isolated"), user_home=default_home
        ),
        enabled=True,
        schedule=RefreshSchedule(refresh_enabled=False, tick_seconds=900),
        host=fake_host(commands, platform=DARWIN_ARM64),
    )
    assert commands.argvs == []


def test_a_non_default_home_does_not_need_either_jobs_policy(
    tmp_path: Path, unit_home: Path
) -> None:
    packages_job.register_packages_job(
        roots=JobRoots(home=unit_home, repo=Path("/isolated"), user_home=tmp_path),
        enabled=True,
        schedule=None,
        host=fake_host(),
    )
    walg_job.register_walg_job(
        roots=JobRoots(home=unit_home, repo=Path("/isolated"), user_home=tmp_path),
        enabled=True,
        backup_hour=None,
        host=fake_host(),
    )


def test_a_registration_with_jobs_on_needs_the_policy_the_root_read(
    tmp_path: Path, default_home: Path
) -> None:
    with pytest.raises(ValueError, match="needs its schedule"):
        packages_job.register_packages_job(
            roots=JobRoots(
                home=default_home / ".meiva", repo=Path("/isolated"), user_home=default_home
            ),
            enabled=True,
            schedule=None,
            host=fake_host(),
        )
    with pytest.raises(ValueError, match="needs the backup hour"):
        walg_job.register_walg_job(
            roots=JobRoots(
                home=default_home / ".meiva", repo=Path("/isolated"), user_home=default_home
            ),
            enabled=True,
            backup_hour=None,
            host=fake_host(),
        )


def _assert_definitions(kind: str, platform: str, first: str, second: str) -> None:
    if kind == "packages":
        assert (
            ("*/20 " in first and "*/30 " in second)
            if platform == "linux"
            else ("<integer>1200</integer>" in first and "<integer>1800</integer>" in second)
        )
    else:
        assert (
            ("25 0 " in first and "25 2 " in second)
            if platform == "linux"
            else ("<integer>0</integer>" in first and "<integer>2</integer>" in second)
        )


@pytest.mark.parametrize("platform", ["mac", "linux"])
@pytest.mark.parametrize("kind", ["packages", "walg"])
def test_real_platform_job_definition_follows_the_value_given(
    default_home: Path,
    platform: str,
    kind: str,
) -> None:
    crontab = FakeCrontab()
    launchctl = FakeCommands()
    launchctl.on("launchctl", Reply(), repeat=True)
    runner: CommandRunner = crontab if platform == "linux" else launchctl
    host = fake_host(
        runner,
        platform=LINUX_X86 if platform == "linux" else DARWIN_ARM64,
        programs={"crontab": "/usr/bin/crontab", "meiva": "/isolated/.venv/bin/meiva"},
    )
    values = {
        "first": 1200 if kind == "packages" else 21,
        "second": 1800 if kind == "packages" else 23,
    }

    def register(owner: str) -> str:
        if kind == "packages":
            packages_job.register_packages_job(
                roots=JobRoots(
                    home=default_home / ".meiva",
                    repo=Path("/isolated"),
                    user_home=default_home,
                ),
                enabled=True,
                schedule=RefreshSchedule(refresh_enabled=True, tick_seconds=values[owner]),
                host=host,
            )
        else:
            walg_job.register_walg_job(
                roots=JobRoots(
                    home=default_home / ".meiva",
                    repo=Path("/isolated"),
                    user_home=default_home,
                ),
                enabled=True,
                backup_hour=values[owner],
                host=host,
            )
        if platform == "linux":
            return crontab.writes[-1]
        label = "packages-refresh" if kind == "packages" else "walg"
        return (default_home / "Library" / "LaunchAgents" / f"com.meiva.{label}.plist").read_text()

    first = register("first")
    second = register("second")
    again = register("first")
    assert first != second
    assert again == first
    operations = [argv[1] for argv in (crontab.argvs if platform == "linux" else launchctl.argvs)]
    assert operations == (["bootout", "bootstrap"] * 3 if platform == "mac" else ["-l", "-"] * 3)
    _assert_definitions(kind, platform, first, second)


@pytest.mark.parametrize(
    "register",
    [
        autostart.register_autostart,
        health_job.register_os_cron,
    ],
)
def test_every_registrar_stops_at_the_switch_before_the_home(
    tmp_path: Path,
    register: Callable[..., None],
) -> None:
    loop = tmp_path / "self-loop"
    loop.symlink_to(loop)
    roots = JobRoots(home=loop, repo=tmp_path, user_home=tmp_path)
    if register is autostart.register_autostart:
        register(services=frozenset({"gateway"}), enabled=False, host=fake_host(), roots=roots)
        with pytest.raises(RuntimeError, match="Symlink loop"):
            register(services=frozenset({"gateway"}), enabled=True, host=fake_host(), roots=roots)
    else:
        register(enabled=False, host=fake_host(), roots=roots)
        with pytest.raises(RuntimeError, match="Symlink loop"):
            register(enabled=True, host=fake_host(), roots=roots)


@pytest.mark.parametrize("kind", ["logs", "packages", "pr-flow", "walg"])
def test_maintenance_switch_precedes_real_home_resolution(tmp_path: Path, kind: str) -> None:
    loop = tmp_path / "self-loop"
    loop.symlink_to(loop)
    roots = JobRoots(home=loop, repo=tmp_path, user_home=tmp_path)

    def register(enabled: bool) -> None:
        match kind:
            case "logs":
                logs_job.register_logs_job(enabled=enabled, roots=roots, host=fake_host())
            case "packages":
                packages_job.register_packages_job(
                    enabled=enabled, schedule=None, roots=roots, host=fake_host()
                )
            case "pr-flow":
                pr_flow_job.register_pr_flow_job(
                    enabled=enabled, production=True, roots=roots, host=fake_host()
                )
            case "walg":
                walg_job.register_walg_job(
                    enabled=enabled, backup_hour=None, roots=roots, host=fake_host()
                )
            case _:
                raise ValueError(kind)

    register(False)
    # meiva:note
    # The enabled call proves this same real path is poisonous at the home boundary;
    # an early return for an ordinary scratch home would not observe the gate order.
    with pytest.raises(RuntimeError, match="Symlink loop"):
        register(True)
