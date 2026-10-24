from collections.abc import Callable
from pathlib import Path

import pytest

from base.host.proc import CommandRunner
from base.host.system import autostart, cron, logs_job, packages_job, pr_flow_job, walg_job
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import (
    DARWIN_ARM64,
    LINUX_X86,
    FakeCommands,
    Reply,
    fake_host,
)
from base.host.system.tests.fake_crontab import FakeCrontab


def _isolated_meiva(*, host: Host) -> str:
    return "/isolated/.venv/bin/meiva"


def _usr_bin(*, host: Host) -> str:
    return "/usr/bin"


def _refuse(message: str) -> Callable[..., None]:
    def refuse(*_args: object, **_kwargs: object) -> None:
        pytest.fail(message)

    return refuse


@pytest.mark.parametrize("enabled", [False, True])
def test_packages_gates_stop_at_the_switch_then_the_home(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    asked: list[str] = []

    def owns(_kind: str) -> bool:
        asked.append("home")
        return False

    monkeypatch.setattr(cron, "owns_os_jobs", owns)
    packages_job.register_packages_job(
        enabled=enabled,
        schedule=packages_job.RefreshSchedule(refresh_enabled=True, tick_seconds=900),
        host=fake_host(),
    )
    assert asked == (["home"] if enabled else [])


def test_packages_refresh_gate_follows_home_and_precedes_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def owns(_kind: str) -> bool:
        return True

    monkeypatch.setattr(cron, "owns_os_jobs", owns)
    monkeypatch.setattr(cron, "register_platform_job", _refuse("platform registrar reached"))
    packages_job.register_packages_job(
        enabled=True,
        schedule=packages_job.RefreshSchedule(refresh_enabled=False, tick_seconds=900),
        host=fake_host(),
    )


def test_a_non_default_home_does_not_need_either_jobs_policy(unit_home: Path) -> None:
    packages_job.register_packages_job(enabled=True, schedule=None, host=fake_host())
    walg_job.register_walg_job(enabled=True, backup_hour=None, host=fake_host())


def test_a_registration_with_jobs_on_needs_the_policy_the_root_read(default_home: Path) -> None:
    with pytest.raises(ValueError, match="needs its schedule"):
        packages_job.register_packages_job(enabled=True, schedule=None, host=fake_host())
    with pytest.raises(ValueError, match="needs the backup hour"):
        walg_job.register_walg_job(enabled=True, backup_hour=None, host=fake_host())


def _isolate_jobs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cron, "job_home", lambda: str(tmp_path / ".meiva"))
    monkeypatch.setattr(cron, "meiva_binary_path", _isolated_meiva)
    monkeypatch.setattr(cron, "launchd_path_env", _usr_bin)

    def owns(_kind: str) -> bool:
        return True

    monkeypatch.setattr(cron, "owns_os_jobs", owns)


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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
    kind: str,
) -> None:
    _isolate_jobs(monkeypatch, tmp_path)
    crontab = FakeCrontab()
    launchctl = FakeCommands()
    launchctl.on("launchctl", Reply(), repeat=True)
    runner: CommandRunner = crontab if platform == "linux" else launchctl
    host = fake_host(
        runner,
        platform=LINUX_X86 if platform == "linux" else DARWIN_ARM64,
        programs={"crontab": "/usr/bin/crontab"},
    )
    values = {
        "first": 1200 if kind == "packages" else 21,
        "second": 1800 if kind == "packages" else 23,
    }

    def register(owner: str) -> str:
        if kind == "packages":
            packages_job.register_packages_job(
                enabled=True,
                schedule=packages_job.RefreshSchedule(
                    refresh_enabled=True, tick_seconds=values[owner]
                ),
                host=host,
            )
        else:
            walg_job.register_walg_job(enabled=True, backup_hour=values[owner], host=host)
        if platform == "linux":
            return crontab.writes[-1]
        label = "packages-refresh" if kind == "packages" else "walg"
        return (tmp_path / "Library" / "LaunchAgents" / f"com.meiva.{label}.plist").read_text()

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
        cron.register_os_cron,
        logs_job.register_logs_job,
        pr_flow_job.register_pr_flow_job,
    ],
)
def test_every_registrar_stops_at_the_switch_before_the_home(
    monkeypatch: pytest.MonkeyPatch,
    register: Callable[..., None],
) -> None:
    def owns(_kind: str) -> bool:
        pytest.fail("home reached")

    monkeypatch.setattr(cron, "owns_os_jobs", owns)
    if register is pr_flow_job.register_pr_flow_job:
        register(enabled=False, production=True, host=fake_host())
    elif register is autostart.register_autostart:
        register(services=frozenset({"gateway"}), enabled=False, host=fake_host())
    else:
        register(enabled=False, host=fake_host())
