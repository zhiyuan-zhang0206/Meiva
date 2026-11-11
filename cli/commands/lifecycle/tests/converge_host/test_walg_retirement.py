# meiva:note
# Retiring WAL-G on a deployed host: the daily job, its files and the binary go; a Postgres that
# still archives keeps the binary until it has been launched again.
#
# The scheduler is the platform stand-in of the other converge tests (`launchctl` replies,
# a fake crontab), the home is real files, and what Postgres reports is handed in as a value.

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from base.host.proc import CommandRunner
from base.host.system.tests.fake_commands import (
    DARWIN_ARM64,
    LINUX_X86,
    FakeCommands,
    Reply,
    fake_host,
)
from base.host.system.tests.fake_crontab import FakeCrontab
from cli.commands.converge.plans.walg_retirement import (
    PostgresArchive,
    binary_may_go,
    deferral_message,
)
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.converge.walg_retirement import execute_walg_retirement
from cli.commands.lifecycle import converge_host

_STOPPED = PostgresArchive(running=False, mode=None)
_NOT_ARCHIVING = PostgresArchive(running=True, mode="off")
_ARCHIVING = PostgresArchive(running=True, mode="on")
_UNREADABLE = PostgresArchive(running=True, mode=None)

_UNRELATED = "0 3 * * * /usr/local/bin/backup"
_OTHER_JOB = "*/20 * * * * /x/meiva packages refresh  # meiva-packages-refresh"
_WALG_JOB = "25 0 * * * /x/meiva backup walg run  # meiva-walg"

_LEFTOVERS = (
    ("runtime", "walg", "wal-g"),
    ("backups", "walg", "state.json"),
    ("backups", "walg", "key-id"),
    ("logs", "walg.log"),
    ("state", "heartbeats", "walg.json"),
)


def _ctx(
    ctx: ConvergeCtx, default_home: Path, scheduler: CommandRunner, *, mac: bool
) -> ConvergeCtx:
    ctx.config.set_field("os_jobs_enabled", True)
    host = fake_host(
        scheduler,
        platform=DARWIN_ARM64 if mac else LINUX_X86,
        programs={"crontab": "/usr/bin/crontab"},
    )
    return replace(ctx, host=host, meiva_home=default_home / ".meiva", roles=None)


def _seed_leftovers(home: Path) -> Path:
    for parts in _LEFTOVERS:
        path = home.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("left behind")
    (home / "state" / "heartbeats" / "logs-maintenance.json").write_text("{}")
    return home.joinpath(*_LEFTOVERS[0])


def _remaining(home: Path) -> list[tuple[str, ...]]:
    return [parts for parts in _LEFTOVERS if home.joinpath(*parts).exists()]


def _launchctl() -> FakeCommands:
    launchctl = FakeCommands()
    launchctl.on("launchctl", Reply(), repeat=True)
    return launchctl


def test_a_host_that_ran_wal_g_loses_the_macos_job_its_files_and_the_binary(
    ctx: ConvergeCtx, default_home: Path
) -> None:
    mac = _ctx(ctx, default_home, _launchctl(), mac=True)
    plist = default_home / "Library" / "LaunchAgents" / "com.meiva.walg.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>")
    _seed_leftovers(mac.meiva_home)

    execute_walg_retirement(mac, archive=_STOPPED)

    assert not plist.exists()
    assert _remaining(mac.meiva_home) == []
    assert (mac.meiva_home / "state" / "heartbeats" / "logs-maintenance.json").exists()


def test_on_linux_only_the_wal_g_crontab_line_goes(ctx: ConvergeCtx, default_home: Path) -> None:
    crontab = FakeCrontab("\n".join([_UNRELATED, _OTHER_JOB, _WALG_JOB]) + "\n")
    linux = _ctx(ctx, default_home, crontab, mac=False)
    _seed_leftovers(linux.meiva_home)

    execute_walg_retirement(linux, archive=_NOT_ARCHIVING)

    assert crontab.lines == [_UNRELATED, _OTHER_JOB]
    assert _remaining(linux.meiva_home) == []


def test_a_postgres_that_still_archives_keeps_the_binary_until_it_is_launched_again(
    ctx: ConvergeCtx, default_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    crontab = FakeCrontab(_WALG_JOB + "\n")
    linux = _ctx(ctx, default_home, crontab, mac=False)
    binary = _seed_leftovers(linux.meiva_home)

    execute_walg_retirement(linux, archive=_ARCHIVING)

    assert [line for line in crontab.lines if line] == [], "the job is gone whatever Postgres does"
    assert _remaining(linux.meiva_home) == [_LEFTOVERS[0]]
    assert binary.exists(), "Postgres would start failing archive attempts without it"
    assert "archive_mode is 'on'" in capsys.readouterr().err

    execute_walg_retirement(linux, archive=_STOPPED)

    assert _remaining(linux.meiva_home) == []


def test_a_postmaster_that_cannot_be_asked_is_treated_as_archiving(
    ctx: ConvergeCtx, default_home: Path
) -> None:
    linux = _ctx(ctx, default_home, FakeCrontab(), mac=False)
    binary = _seed_leftovers(linux.meiva_home)

    execute_walg_retirement(linux, archive=_UNREADABLE)

    assert binary.exists()


def test_a_host_that_never_ran_wal_g_is_left_alone_and_a_repeat_changes_nothing(
    ctx: ConvergeCtx, default_home: Path
) -> None:
    crontab = FakeCrontab("\n".join([_UNRELATED, _OTHER_JOB]) + "\n")
    linux = _ctx(ctx, default_home, crontab, mac=False)

    execute_walg_retirement(linux, archive=_STOPPED)
    execute_walg_retirement(linux, archive=_STOPPED)

    assert crontab.lines == [_UNRELATED, _OTHER_JOB]


def test_a_scratch_home_leaves_the_hosts_job_but_still_cleans_its_own_files(
    ctx: ConvergeCtx, default_home: Path, tmp_path: Path
) -> None:
    crontab = FakeCrontab(_WALG_JOB + "\n")
    scratch = replace(
        _ctx(ctx, default_home, crontab, mac=False), meiva_home=tmp_path / "scratch-home"
    )
    _seed_leftovers(scratch.meiva_home)

    execute_walg_retirement(scratch, archive=_STOPPED)

    assert crontab.lines == [_WALG_JOB]
    assert _remaining(scratch.meiva_home) == []


@pytest.mark.parametrize(
    ("state", "goes"),
    [(_STOPPED, True), (_NOT_ARCHIVING, True), (_ARCHIVING, False), (_UNREADABLE, False)],
)
def test_the_binary_goes_only_when_postgres_is_known_not_to_archive(
    state: PostgresArchive, goes: bool
) -> None:
    assert binary_may_go(state) is goes


def test_the_deferral_names_how_to_finish() -> None:
    assert "meiva stop --stop-infra" in deferral_message(_ARCHIVING)
    assert "could not be read" in deferral_message(_UNREADABLE)


def test_the_step_runs_on_gateways_after_the_postgres_runtime_and_before_the_pooler() -> None:
    names = [step.name for step in converge_host.CONVERGE_STEPS]
    step = next(s for s in converge_host.CONVERGE_STEPS if s.name == "retire WAL-G archiving")

    assert step.roles == frozenset({"gateway"})
    assert names.index("PostgreSQL 17 + pgvector runtime") < names.index(step.name)
    assert names.index(step.name) < names.index("one DB URL + pgbouncer binary (when enabled)")
