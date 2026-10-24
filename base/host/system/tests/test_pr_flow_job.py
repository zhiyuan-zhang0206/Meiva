"""PR-flow sampler OS job — registration contract, gating, and definitions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from base.host.system import cron
from base.host.system import pr_flow_job as job
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
_GH = {"gh": "/opt/homebrew/bin/gh"}


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


@pytest.fixture(autouse=True)
def _configure_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))


def test_macos_reregistration_rewrites_and_reloads_idempotently(
    job_roots: JobRoots,
) -> None:
    commands, host = _launchctl()

    assert job.register_macos(roots=job_roots, host=host) == 0
    plist = job.launchd_plist_path()
    first = plist.read_text(encoding="utf-8")
    assert job.register_macos(roots=job_roots, host=host) == 0

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
    old = "25 0 * * * /old/meiva  # meiva-pr-flow"
    crontab = FakeCrontab(f"{other}\n\n{old}\n{old}\n")

    assert job.register_linux(roots=job_roots, host=_linux(crontab)) == 0
    body = crontab.writes[-1]
    assert other in body
    assert f"{other}\n\n" in body
    assert old not in body
    assert body.count("# meiva-pr-flow") == 1
    assert "25 0 * * *" in body
    assert "pr_flow_export.py" in body
    assert str(tmp_path / ".meiva" / "logs" / "pr-flow.out.log") in body


def test_linux_crontab_failures_and_empty_table(
    job_roots: JobRoots, capsys: pytest.CaptureFixture[str]
) -> None:
    assert job.register_linux(roots=job_roots, host=fake_host(platform=LINUX_X86)) == 1
    assert capsys.readouterr().err == (
        "  * PR flow: crontab not installed; the daily sampler job cannot be registered\n"
    )

    crontab = FakeCrontab("stale")
    crontab.read_returncode = 1
    crontab.read_stderr = "permission denied"
    host = _linux(crontab)
    assert job.register_linux(roots=job_roots, host=host) == 1
    assert crontab.writes == []
    assert capsys.readouterr().err == (
        "  * crontab -l failed (permission denied); "
        "skipping PR-flow registration to avoid clobbering the crontab\n"
    )

    crontab.read_stderr = "no crontab for user"
    assert job.register_linux(roots=job_roots, host=host) == 0
    assert (
        len(crontab.writes),
        crontab.writes[0].startswith("25 0 * * * "),
        crontab.writes[0].endswith("  # meiva-pr-flow\n"),
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors: list[tuple[object, ...]] = []

    def record_error(*args: object) -> None:
        errors.append(args)

    commands, host = _launchctl(bootstrap=Reply(stderr="denied", returncode=1))
    monkeypatch.setattr(job.logger, "error", record_error)
    assert job.register_macos(roots=job_roots, host=host) == 1
    assert [argv[1] for argv in commands.argvs if argv[1] != "print"] == ["bootout", "bootstrap"]
    assert errors == [("launchctl bootstrap failed for {}: {}", "com.meiva.pr-flow", "denied")]

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


def _passing_credentials(tmp_path: Path) -> Host:
    """A host with `gh` on its PATH and a Trunk token on disk."""
    token = tmp_path / ".trunk" / "api-token"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("tok\n", encoding="utf-8")
    return fake_host(programs=_GH)


def test_credential_blocker_names_each_missing_piece(job_roots: JobRoots, tmp_path: Path) -> None:
    assert (
        job.credential_blocker(roots=job_roots, production=False, host=fake_host())
        == "not the registered production home"
    )

    assert (
        job.credential_blocker(roots=job_roots, production=True, host=fake_host())
        == "gh CLI not on PATH"
    )

    with_gh = fake_host(programs=_GH)
    assert (
        job.credential_blocker(roots=job_roots, production=True, host=with_gh)
        == f"no Trunk API token at {tmp_path}/.trunk/api-token"
    )

    token = tmp_path / ".trunk" / "api-token"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("   \n", encoding="utf-8")
    assert "no Trunk API token" in (
        job.credential_blocker(roots=job_roots, production=True, host=with_gh) or ""
    )

    token.write_text("tok\n", encoding="utf-8")
    assert job.credential_blocker(roots=job_roots, production=True, host=with_gh) is None


def test_register_skips_when_os_jobs_are_disabled(
    job_roots: JobRoots, monkeypatch: pytest.MonkeyPatch
) -> None:
    skipped: list[str] = []
    monkeypatch.setattr(cron, "skip_os_job", skipped.append)

    def no_platform(*_a: object, **_k: object) -> None:
        raise AssertionError("the platform must not be touched with OS jobs off")

    monkeypatch.setattr(cron, "register_platform_job", no_platform)
    job.register_pr_flow_job(roots=job_roots, enabled=False, production=True, host=fake_host())
    assert skipped == ["pr flow"]


def test_register_skips_without_credentials(
    job_roots: JobRoots,
    default_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_platform(*_a: object, **_k: object) -> None:
        raise AssertionError("the platform must not be touched without credentials")

    monkeypatch.setattr(cron, "register_platform_job", no_platform)
    job.register_pr_flow_job(
        roots=replace(job_roots, home=default_home / ".meiva", user_home=default_home),
        enabled=True,
        production=True,
        host=fake_host(),
    )


def test_register_delegates_when_credentials_pass(
    job_roots: JobRoots,
    default_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _passing_credentials(default_home)
    calls: list[str] = []
    monkeypatch.setattr(cron, "register_platform_job", _recorder(calls, "register"))
    monkeypatch.setattr(cron, "unregister_platform_job", _recorder(calls, "unregister"))

    job.register_pr_flow_job(
        roots=replace(job_roots, home=default_home / ".meiva", user_home=default_home),
        enabled=True,
        production=True,
        host=host,
    )
    assert calls == ["register"]

    job.unregister_pr_flow_job(host=host)
    assert calls == ["register", "unregister"]
