"""Recurring content-refresh OS job registration contract."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from base.host.system import packages_content as definition
from base.host.system import packages_job as job
from base.host.system.cron import mechanics
from base.host.system.hostport import Host
from base.host.system.job_values import JobRoots
from base.host.system.tests.fake_commands import (
    DARWIN_ARM64,
    LINUX_X86,
    FakeCommands,
    Reply,
    fake_host,
)
from base.host.system.tests.fake_crontab import FakeCrontab

_UID = 501


_NOT_LOADED = Reply(returncode=113, stderr="Could not find service")


def _launchctl(**replies: Reply) -> tuple[FakeCommands, Host]:
    """A macOS host whose launchctl verbs answer as given (default: success)."""
    commands = FakeCommands()
    commands.on(["launchctl", "print"], replies.pop("print", _NOT_LOADED), repeat=True)
    for verb, reply in replies.items():
        commands.on(["launchctl", verb], reply, repeat=True)
    commands.on("launchctl", Reply(), repeat=True)
    return commands, fake_host(
        commands, platform=DARWIN_ARM64, uid=_UID, programs={"meiva": "/work tree/.venv/bin/meiva"}
    )


def _linux(crontab: FakeCrontab) -> Host:
    return fake_host(
        crontab,
        platform=LINUX_X86,
        programs={"crontab": "/usr/bin/crontab", "meiva": "/work tree/.venv/bin/meiva"},
    )


def _recorder(calls: list[str], label: str) -> Callable[..., None]:
    def record(*_args: object, **_kwargs: object) -> None:
        calls.append(label)

    return record


def _refuse(message: str) -> Callable[..., None]:
    def refuse(*_args: object, **_kwargs: object) -> None:
        pytest.fail(message)

    return refuse


@pytest.fixture(autouse=True)
def _configure_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))


def test_macos_reregistration_rewrites_and_reloads_idempotently(
    job_roots: JobRoots,
) -> None:
    commands, host = _launchctl()

    assert job.register_macos(roots=job_roots, tick_seconds=900, host=host) == 0
    plist = job.launchd_plist_path()
    first = plist.read_text(encoding="utf-8")
    assert job.register_macos(roots=job_roots, tick_seconds=900, host=host) == 0

    assert plist.read_text(encoding="utf-8") == first
    assert [argv[1] for argv in commands.argvs if argv[1] != "print"] == [
        "bootout",
        "bootstrap",
        "bootout",
        "bootstrap",
    ]


def test_linux_registration_replaces_only_its_own_lines(
    job_roots: JobRoots, tmp_path: Path
) -> None:
    other = "40 4 * * * /x/meiva logs rotate  # meiva-logs-maintenance"
    old = "*/15 * * * * /old/meiva packages refresh --from-job  # meiva-packages-refresh"
    crontab = FakeCrontab(f"{other}\n\n{old}\n{old}\n")

    assert job.register_linux(roots=job_roots, tick_seconds=900, host=_linux(crontab)) == 0
    body = crontab.writes[-1]
    assert other in body
    assert f"{other}\n\n" in body
    assert old not in body
    assert body.count("# meiva-packages-refresh") == 1
    assert "*/15 * * * *" in body
    assert "packages refresh --from-job" in body
    assert str(tmp_path / ".meiva" / "logs" / "packages-refresh.log") in body


def test_linux_crontab_failures_and_empty_table(
    job_roots: JobRoots, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        job.register_linux(
            roots=job_roots,
            tick_seconds=900,
            host=fake_host(platform=LINUX_X86),
        )
        == 1
    )
    assert capsys.readouterr().err == (
        "  * packages refresh: crontab not installed; the recurring refresh "
        "pass cannot be registered\n"
    )

    crontab = FakeCrontab("stale")
    crontab.read_returncode = 1
    crontab.read_stderr = "permission denied"
    host = _linux(crontab)
    assert job.register_linux(roots=job_roots, tick_seconds=900, host=host) == 1
    assert crontab.writes == []
    assert capsys.readouterr().err == (
        "  * crontab -l failed (permission denied); "
        "skipping packages-refresh registration to avoid clobbering the crontab\n"
    )

    crontab.read_stderr = "no crontab for user"
    assert job.register_linux(roots=job_roots, tick_seconds=900, host=host) == 0
    assert (
        len(crontab.writes),
        crontab.writes[0].startswith("*/15 * * * * "),
        crontab.writes[0].endswith("  # meiva-packages-refresh\n"),
        crontab.writes[0].count("\n"),
    ) == (1, True, True, 1)

    crontab.read_returncode = 0
    assert (
        job.unregister_linux(host=host),
        crontab.writes[-1],
        job.unregister_linux(host=host),
        len(crontab.writes),
    ) == (0, "\n", 0, 2)

    crontab.table = crontab.writes[0]
    crontab.write_failure = "write denied"
    assert job.register_linux(roots=job_roots, tick_seconds=900, host=host) == 1
    assert capsys.readouterr().err == "  * crontab update failed: write denied\n"
    assert job.unregister_linux(host=host) == 1
    assert len(crontab.writes) == 2


def test_macos_bootstrap_failure_and_repeated_unregister(
    job_roots: JobRoots,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors: list[tuple[object, ...]] = []

    def record_error(*args: object) -> None:
        errors.append(args)

    commands, host = _launchctl(bootstrap=Reply(stderr="denied", returncode=1))
    monkeypatch.setattr(job.logger, "error", record_error)
    assert job.register_macos(roots=job_roots, tick_seconds=900, host=host) == 1
    assert [argv[1] for argv in commands.argvs if argv[1] != "print"] == ["bootout", "bootstrap"]
    assert errors == [
        ("launchctl bootstrap failed for {}: {}", "com.meiva.packages-refresh", "denied")
    ]

    plist = job.launchd_plist_path()
    assert plist.exists()
    assert job.unregister_macos(host=host) == 0
    assert job.unregister_macos(host=host) == 0
    assert not plist.exists()
    assert [argv[1] for argv in commands.argvs if argv[1] != "print"] == [
        "bootout",
        "bootstrap",
        "bootout",
        "bootout",
    ]


def test_register_is_gated_by_os_jobs(job_roots: JobRoots, monkeypatch: pytest.MonkeyPatch) -> None:
    skipped: list[str] = []
    monkeypatch.setattr(mechanics, "skip_os_job", skipped.append)
    monkeypatch.setattr(
        mechanics,
        "register_platform_job",
        _refuse("registration reached the platform with the gate off"),
    )

    job.register_packages_job(
        roots=job_roots,
        enabled=False,
        schedule=None,
        host=fake_host(),
    )
    assert skipped == ["packages refresh"]


def test_register_is_skipped_when_refresh_disabled(
    job_roots: JobRoots, default_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        mechanics,
        "register_platform_job",
        _refuse("registration reached the platform with refresh disabled"),
    )

    job.register_packages_job(
        roots=replace(job_roots, home=default_home / ".meiva", user_home=default_home),
        enabled=True,
        schedule=definition.RefreshSchedule(refresh_enabled=False, tick_seconds=900),
        host=fake_host(),
    )


def test_register_dispatches_to_the_platform(
    job_roots: JobRoots, default_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def register(what: str, **_platforms: object) -> None:
        calls.append(f"register {what}")

    monkeypatch.setattr(mechanics, "register_platform_job", register)

    job.register_packages_job(
        roots=replace(job_roots, home=default_home / ".meiva", user_home=default_home),
        enabled=True,
        schedule=definition.RefreshSchedule(refresh_enabled=True, tick_seconds=900),
        host=fake_host(),
    )
    assert calls == ["register packages-refresh"]


def test_unregister_delegates_to_the_platform(
    default_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(mechanics, "unregister_platform_job", _recorder(calls, "unregister"))

    job.unregister_packages_job(host=fake_host())
    assert calls == ["unregister"]
