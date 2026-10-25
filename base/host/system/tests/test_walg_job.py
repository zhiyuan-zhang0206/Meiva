"""WAL-G daily tick OS job: the definition it writes, and when it reaches the scheduler."""

from __future__ import annotations

import shlex
from dataclasses import replace
from pathlib import Path

import pytest

from base.host.system import walg_job as job
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
from base.log.tests.log_records import LogRecords

_UID = 501


_NOT_LOADED = Reply(returncode=113, stderr="Could not find service")


def _launchctl() -> tuple[FakeCommands, Host]:
    """A macOS host whose launchctl always succeeds, and the commands it was asked."""
    commands = FakeCommands()
    commands.on(["launchctl", "print"], _NOT_LOADED, repeat=True)
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


def test_macos_reregistration_rewrites_and_reloads_idempotently(
    job_roots: JobRoots,
) -> None:
    commands, host = _launchctl()

    assert job.register_macos(roots=job_roots, backup_hour=3, host=host) == 0
    plist = job.launchd_plist_path(user_home=job_roots.user_home)
    first = plist.read_text(encoding="utf-8")
    assert job.register_macos(roots=job_roots, backup_hour=3, host=host) == 0

    assert plist.read_text(encoding="utf-8") == first
    assert [argv[1] for argv in commands.argvs if argv[1] != "print"] == [
        "bootout",
        "bootstrap",
        "bootout",
        "bootstrap",
    ]


def test_macos_unregister_removes_the_plist_and_is_repeatable(
    job_roots: JobRoots,
) -> None:
    commands, host = _launchctl()
    assert job.register_macos(roots=job_roots, backup_hour=3, host=host) == 0
    plist = job.launchd_plist_path(user_home=job_roots.user_home)
    assert plist.exists()

    assert (
        job.unregister_macos(host=host, user_home=job_roots.user_home),
        job.unregister_macos(host=host, user_home=job_roots.user_home),
    ) == (0, 0)

    assert not plist.exists()
    assert [argv[1] for argv in commands.argvs if argv[1] != "print"][-2:] == ["bootout", "bootout"]


def test_linux_registration_replaces_only_its_own_line(job_roots: JobRoots, tmp_path: Path) -> None:
    other = "40 4 * * * /x/meiva logs rotate  # meiva-logs-maintenance"
    old = "25 1 * * * /old/meiva backup walg run  # meiva-walg"
    crontab = FakeCrontab(f"{other}\n\n{old}\n{old}\n")

    assert job.register_linux(roots=job_roots, backup_hour=3, host=_linux(crontab)) == 0

    body = crontab.writes[-1]
    assert f"{other}\n\n" in body
    assert old not in body
    assert body.count("# meiva-walg") == 1
    (line,) = [line for line in body.splitlines() if "# meiva-walg" in line]
    assert line.startswith("25 6 * * * ")
    # `/bin/sh -c <quoted command>`: the shell word after -c is the command itself.
    command = shlex.split(line.split(" /bin/sh -c ", 1)[1])[0]
    assert command == "'/work tree/.venv/bin/meiva' backup walg run"
    assert f">> {tmp_path}/.meiva/logs/walg.log 2>&1" in line
    assert line.endswith("  # meiva-walg")


def test_linux_without_crontab_reports_and_registers_nothing(
    job_roots: JobRoots,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        job.register_linux(
            roots=job_roots,
            backup_hour=3,
            host=fake_host(platform=LINUX_X86),
        )
        == 1
    )
    assert capsys.readouterr().err == (
        "  * WAL-G backup: crontab not installed; the daily tick cannot be registered\n"
    )


def test_linux_unregister_removes_the_line_and_keeps_the_rest() -> None:
    other = "40 4 * * * /x/meiva logs rotate  # meiva-logs-maintenance"
    crontab = FakeCrontab(f"{other}\n25 6 * * * /x/meiva backup walg run  # meiva-walg\n")

    assert job.unregister_linux(host=_linux(crontab)) == 0

    assert crontab.table == other + "\n"


def test_register_skips_when_os_jobs_are_disabled(
    job_roots: JobRoots, loguru_records: LogRecords
) -> None:
    commands = FakeCommands()
    job.register_walg_job(
        roots=job_roots,
        enabled=False,
        backup_hour=None,
        host=fake_host(commands, platform=DARWIN_ARM64),
    )
    assert commands.argvs == []
    assert any(
        "OS jobs disabled (MEIVA_OS_JOBS_ENABLED=false) — not registering WAL-G tick"
        in r["message"]
        for r in loguru_records
    )


def test_register_and_unregister_delegate_in_the_default_home(
    job_roots: JobRoots, default_home: Path
) -> None:
    commands, host = _launchctl()

    job.register_walg_job(
        roots=replace(job_roots, home=default_home / ".meiva", user_home=default_home),
        enabled=True,
        backup_hour=3,
        host=host,
    )
    plist = job.launchd_plist_path(user_home=default_home)
    assert plist.exists()
    job.unregister_walg_job(host=host, user_home=default_home)
    assert not plist.exists()
    assert [argv[1] for argv in commands.argvs if argv[1] != "print"] == [
        "bootout",
        "bootstrap",
        "bootout",
    ]
