"""Daily rotate-then-retention OS job registration contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from base.host.system import logs_content
from base.host.system import logs_job as job
from base.host.system.hostport import Host
from base.host.system.job_values import JobRoots, plist_path
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


def test_macos_reregistration_rewrites_and_reloads_idempotently(
    job_roots: JobRoots,
) -> None:
    commands, host = _launchctl()

    assert job.register_macos(roots=job_roots, host=host) == 0
    plist = plist_path(logs_content.LABEL, user_home=job_roots.user_home)
    first = plist.read_text(encoding="utf-8")
    assert job.register_macos(roots=job_roots, host=host) == 0

    assert plist.read_text(encoding="utf-8") == first
    assert [argv[1] for argv in commands.argvs if argv[1] != "print"] == [
        "bootout",
        "bootstrap",
        "bootout",
        "bootstrap",
    ]


def test_macos_unregister_removes_only_its_own_job(tmp_path: Path) -> None:
    target = plist_path(logs_content.LABEL, user_home=tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text("<target-plist/>", encoding="utf-8")
    other = target.parent / "com.meiva.packages-refresh.plist"
    other.write_text("<other-plist/>", encoding="utf-8")
    commands, host = _launchctl()

    assert job.unregister_macos(host=host, user_home=tmp_path) == 0

    assert not target.exists()
    assert other.read_bytes() == b"<other-plist/>"
    assert commands.argvs == [
        ("launchctl", "bootout", f"gui/{_UID}/com.meiva.logs-maintenance"),
        ("launchctl", "print", f"gui/{_UID}/com.meiva.logs-maintenance"),
    ]


def test_linux_registration_replaces_only_its_own_lines(
    job_roots: JobRoots,
) -> None:
    other = "*/15 * * * * /x/meiva packages refresh --from-job  # meiva-packages-refresh"
    old = "35 4 * * * /old/meiva logs retention  # meiva-logs-maintenance"
    crontab = FakeCrontab(f"{other}\n\n{old}\n{old}\n")

    assert job.register_linux(roots=job_roots, host=_linux(crontab)) == 0
    body = crontab.writes[-1]
    assert other in body
    assert f"{other}\n\n" in body
    assert old not in body
    assert body.count("# meiva-logs-maintenance") == 1
    assert "40 4 * * *" in body
    assert "logs rotate" in body and "logs retention" in body


def test_linux_crontab_failures_and_empty_table(
    job_roots: JobRoots, capsys: pytest.CaptureFixture[str]
) -> None:
    assert job.register_linux(roots=job_roots, host=fake_host(platform=LINUX_X86)) == 1
    assert capsys.readouterr().err == (
        "  * logs maintenance: crontab not installed; daily rotation and "
        "retention cannot be registered\n"
    )

    crontab = FakeCrontab("stale")
    crontab.read_returncode = 1
    crontab.read_stderr = "permission denied"
    host = _linux(crontab)
    assert job.register_linux(roots=job_roots, host=host) == 1
    assert crontab.writes == []
    assert capsys.readouterr().err == (
        "  * crontab -l failed (permission denied); "
        "skipping logs-maintenance registration to avoid clobbering the crontab\n"
    )

    crontab.read_stderr = "no crontab for user"
    assert job.register_linux(roots=job_roots, host=host) == 0
    assert (
        len(crontab.writes),
        crontab.writes[0].startswith("40 4 * * * "),
        crontab.writes[0].endswith("  # meiva-logs-maintenance\n"),
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
    assert job.register_linux(roots=job_roots, host=host) == 1
    assert capsys.readouterr().err == "  * crontab update failed: write denied\n"
    assert job.unregister_linux(host=host) == 1
    assert len(crontab.writes) == 2


def test_macos_bootstrap_failure_and_repeated_unregister(
    job_roots: JobRoots,
    loguru_records: LogRecords,
) -> None:
    commands, host = _launchctl(bootstrap=Reply(stderr="denied", returncode=1))
    assert job.register_macos(roots=job_roots, host=host) == 1
    assert [argv[1] for argv in commands.argvs if argv[1] != "print"] == ["bootout", "bootstrap"]
    assert any(
        r["level"].name == "ERROR"
        and r["message"] == "launchctl bootstrap failed for com.meiva.logs-maintenance: denied"
        for r in loguru_records
    )

    plist = plist_path(logs_content.LABEL, user_home=job_roots.user_home)
    assert plist.exists()
    assert job.unregister_macos(host=host, user_home=job_roots.user_home) == 0
    assert job.unregister_macos(host=host, user_home=job_roots.user_home) == 0
    assert not plist.exists()
    assert [argv[1] for argv in commands.argvs if argv[1] != "print"] == [
        "bootout",
        "bootstrap",
        "bootout",
        "bootout",
    ]
