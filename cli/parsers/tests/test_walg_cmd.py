# meiva:note
# `meiva backup walg check|run|restore|status`: what an operator reads, and the parser that reaches it.

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cli.commands.data_plane import walg as walg_cmd
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.tests.data_plane_records import data_plane_config as data_plane_config
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime
from cli.parsers import build_parser
from services.backup.walg import config as walg_config
from services.backup.walg import probe, state
from services.backup.walg.tests.support import (
    SECRETS,
    Sandbox,
    archive_current_segment,
    archiving_postgres,
    make_sandbox,
    take_basebackup,
)


@pytest.fixture
def plane_off(data_plane_config: DataPlaneConfig) -> DataPlaneConfig:
    """The data-plane values of a home where WAL-G is not switched on."""
    return dataclasses.replace(data_plane_config, walg_config_file=None)


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    return make_sandbox(tmp_path, monkeypatch)


@pytest.fixture
def plane(data_plane_config: DataPlaneConfig, sandbox: Sandbox) -> DataPlaneConfig:
    """The data-plane values of the sandbox home: WAL-G is on and reads the sandbox's file."""
    return dataclasses.replace(data_plane_config, walg_config_file=sandbox.config_file)


def test_the_parser_reaches_every_verb() -> None:
    parser = build_parser()

    check = parser.parse_args(["backup", "walg", "check"])
    run = parser.parse_args(["backup", "walg", "run"])
    status = parser.parse_args(["backup", "walg", "status"])
    restore = parser.parse_args(["backup", "walg", "restore", "--dir", "/srv/restored"])
    drill = parser.parse_args(["backup", "walg", "drill"])

    assert drill.func.__name__ == "_h_backup_walg_drill"

    assert restore.func.__name__ == "_h_backup_walg_restore"
    assert (restore.dir, restore.backup, restore.time, restore.lsn, restore.user) == (
        "/srv/restored",
        "LATEST",
        None,
        None,
        None,
    )
    named = parser.parse_args(["backup", "walg", "restore", "--dir", "/d", "--user", "zyonzhang"])
    assert named.user == "zyonzhang"
    assert check.func.__name__ == "_h_backup_walg_check"
    assert run.func.__name__ == "_h_backup_walg_run"
    assert status.func.__name__ == "_h_backup_walg_status"


_STAMPED = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \S")


# meiva:note
# The parser's handler is the verb's door: it reads the invocation's own configuration owner (the
# command runtime's) and hands the verb values.
def _at_the_door(
    argv: list[str],
    command: CommandRuntime,
    database_factory: OperatorDatabaseFactory,
    *,
    walg_config_file: Path | None,
) -> int:
    command.config.set_field("walg_config_file", walg_config_file)
    args = build_parser().parse_args(argv)
    args.runtime = command
    args.database_factory = database_factory
    return int(args.func(args))


def test_run_prints_timestamped_lines_and_returns_the_ticks_exit_code(
    sandbox: Sandbox,
    capsys: pytest.CaptureFixture[str],
    command_runtime: CommandRuntime,
    operator_database: OperatorDatabaseFactory,
) -> None:
    code = _at_the_door(
        ["backup", "walg", "run"],
        command_runtime,
        operator_database,
        walg_config_file=sandbox.config_file,
    )

    assert code == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines
    assert all(_STAMPED.match(line) for line in lines)


def test_status_says_when_the_tick_never_ran(
    plane: DataPlaneConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert walg_cmd.cmd_walg_status(plane) == 0

    assert "daily tick: never ran" in capsys.readouterr().out


def test_status_shows_the_last_run_and_its_failed_step(
    plane: DataPlaneConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    started = datetime(2026, 10, 2, 6, 25, tzinfo=UTC)
    state.write_state(
        state.State(
            tick=state.TickRecord(started_at=started),
            run=state.RunRecord(
                started_at=started,
                finished_at=started,
                status="failed",
                step="verify",
                detail="the archived WAL chain is broken",
            ),
            verify=state.VerifyRecord(at=started, integrity="FAILURE", timeline="OK"),
        )
    )

    assert walg_cmd.cmd_walg_status(plane) == 0

    out = capsys.readouterr().out
    assert "daily tick: last started 2026-10-02T06:25:00+00:00" in out
    assert "last run: failed at verify (2026-10-02T06:25:00+00:00): the archived WAL chain" in out
    assert "last verify: integrity FAILURE, timeline OK" in out


def test_status_reports_an_unreadable_state_file(
    plane: DataPlaneConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    state.state_path().parent.mkdir(parents=True)
    state.state_path().write_text("{broken")

    assert walg_cmd.cmd_walg_status(plane) == 0

    assert "daily tick: state UNREADABLE" in capsys.readouterr().out


def test_check_prints_a_mark_per_step_and_exits_zero_when_all_pass(
    plane: DataPlaneConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert walg_cmd.cmd_walg_check(plane, token="e" * 32) == 0

    out = capsys.readouterr().out
    assert [line.split(":")[0] for line in out.splitlines()] == [
        "  ✓ configured",
        "  ✓ binary",
        "  ✓ configuration",
        "  ✓ storage",
        "  ✓ postgres",
    ]
    assert not any(secret in out for secret in SECRETS)


def test_check_exits_non_zero_and_names_the_failing_step(
    sandbox: Sandbox, plane: DataPlaneConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    sandbox.set_mode("nodelete")

    assert walg_cmd.cmd_walg_check(plane, token="f" * 32) == 1

    assert "  ✗ storage: cannot delete" in capsys.readouterr().out


def test_status_when_off_says_so(
    plane_off: DataPlaneConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert walg_cmd.cmd_walg_status(plane_off) == 0

    assert capsys.readouterr().out == "WAL-G archiving is off (MEIVA_WALG_CONFIG_FILE is not set)\n"


def test_status_at_the_door_reads_the_path_from_the_runtime_configuration(
    sandbox: Sandbox,
    capsys: pytest.CaptureFixture[str],
    command_runtime: CommandRuntime,
    operator_database: OperatorDatabaseFactory,
) -> None:
    fingerprint = walg_config.read_config(sandbox.config_file).key_fingerprint

    code = _at_the_door(
        ["backup", "walg", "status"],
        command_runtime,
        operator_database,
        walg_config_file=sandbox.config_file,
    )

    assert code == 0
    out = capsys.readouterr().out
    assert f"config file: {sandbox.config_file}" in out
    assert f"key fingerprint: {fingerprint} (pinned: not yet)" in out


def test_status_at_the_door_says_so_when_the_runtime_configuration_has_no_path(
    capsys: pytest.CaptureFixture[str],
    command_runtime: CommandRuntime,
    operator_database: OperatorDatabaseFactory,
) -> None:
    code = _at_the_door(
        ["backup", "walg", "status"], command_runtime, operator_database, walg_config_file=None
    )

    assert code == 0
    assert capsys.readouterr().out == "WAL-G archiving is off (MEIVA_WALG_CONFIG_FILE is not set)\n"


def test_status_shows_the_fingerprint_not_the_key(
    sandbox: Sandbox, plane: DataPlaneConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    fingerprint = walg_config.read_config(sandbox.config_file).key_fingerprint

    assert walg_cmd.cmd_walg_status(plane) == 0

    out = capsys.readouterr().out
    assert f"key fingerprint: {fingerprint} (pinned: not yet)" in out
    assert "prefix: oss://meiva-backups/meiva-walg/test-home/pg17/gen1/" in out
    assert "postgres: archiver state not read" in out
    assert "health: WAL archiving: the archiver state is unreadable" in out
    assert not any(secret in out for secret in SECRETS)


def test_status_reports_an_unusable_configuration_instead_of_raising(
    sandbox: Sandbox, plane: DataPlaneConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    sandbox.config_file.write_text("{broken")

    assert walg_cmd.cmd_walg_status(plane) == 0

    out = capsys.readouterr().out
    assert "configuration: UNUSABLE" in out
    assert "health: WAL archiving: the WAL-G configuration is unusable" in out


# meiva:note
# The retained-postmaster case: archiving was configured after Postgres launched.
def test_a_postgres_that_kept_its_old_launch_arguments_is_flagged_at_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    postgres_access: None,
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch, enabled=False)
    with archiving_postgres() as pg:
        walg_cmd.warn_archive_inactive_on(sandbox.config_file, probe.admin_connection_to(pg.target))

    err = capsys.readouterr().err
    assert "this Postgres is not running with the configured archive settings" in err
    assert "`meiva stop` and `meiva start`" in err


def test_a_postgres_launched_with_the_arguments_is_not_flagged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    postgres_access: None,
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch)
    with archiving_postgres() as pg:
        walg_cmd.warn_archive_inactive_on(sandbox.config_file, probe.admin_connection_to(pg.target))

    assert capsys.readouterr().err == ""


def test_nothing_is_dialed_while_wal_g_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_sandbox(tmp_path, monkeypatch, enabled=False)

    walg_cmd.warn_archive_inactive(None)

    assert capsys.readouterr().err == ""


# meiva:note
# The sandbox home has no registry record, so there is no Postgres to dial: the command says why
# it could not read the archiver and carries on.
def test_an_unreadable_postgres_is_a_warning_not_a_failure(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    walg_cmd.warn_archive_inactive(sandbox.config_file)

    err = capsys.readouterr().err
    assert "WAL archiving state not read (RuntimeError: no registry record for home" in err
    assert "cannot dial its Postgres" in err


# meiva:note
# The command restores for real: a Postgres archiving into the sandbox's WAL-G stand-in, a base
# backup the stand-in serves back, WAL up to a point after the target, and the command's own
# arguments -- the backup, the target LSN, the restored cluster's superuser -- steering a scratch
# recovery. The directory is left as a promoted database whose new timeline forked at the target.
def test_restore_recovers_to_the_target_and_keeps_the_data_directory(
    sandbox: Sandbox,
    plane: DataPlaneConfig,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    postgres_access: None,
) -> None:
    with archiving_postgres(sandbox=sandbox) as pg:
        conn = pg.connect()
        conn.execute("CREATE TABLE t (k text)")
        conn.execute("INSERT INTO t VALUES ('A')")
        backup = take_basebackup(sandbox, pg, "base_000000010000000000000001")
        archive_current_segment(conn, sandbox)
        conn.execute("INSERT INTO t VALUES ('B1')")
        archive_current_segment(conn, sandbox)
        target_row = conn.execute("SELECT pg_current_wal_insert_lsn()").fetchone()
        assert target_row is not None
        target = str(target_row[0])
        conn.execute("INSERT INTO t VALUES ('B2')")
        archive_current_segment(conn, sandbox)
        conn.close()
        destination = tmp_path / "out"

        code = walg_cmd.cmd_walg_restore(
            plane, directory=str(destination), backup=backup, time=None, lsn=target, user="meiva"
        )

    assert code == 0
    out = capsys.readouterr().out
    assert "Postgres is not running on it" in out
    assert (destination / "PG_VERSION").is_file()
    assert not (destination / "postmaster.pid").exists()
    assert not (destination / "recovery.signal").exists()
    assert f"backup-fetch {destination.resolve()} {backup}" in sandbox.calls()
    history = (destination / "pg_wal" / "00000002.history").read_text()
    assert f"after LSN {target}" in history


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"time": None, "lsn": "not-an-lsn"}, "is not an LSN"),
        ({"time": "2026-10-01", "lsn": "0/1"}, "not both"),
    ],
)
def test_restore_rejects_a_bad_target_before_fetching(
    sandbox: Sandbox,
    plane: DataPlaneConfig,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    kwargs: dict[str, Any],
    message: str,
) -> None:
    code = walg_cmd.cmd_walg_restore(
        plane, directory=str(tmp_path / "out"), backup="LATEST", **kwargs
    )

    assert code == 1
    assert message in capsys.readouterr().err
    assert not sandbox.calls()


def test_restore_reports_a_failed_fetch_and_exits_non_zero(
    sandbox: Sandbox, plane: DataPlaneConfig, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    sandbox.fail("backup-fetch")

    code = walg_cmd.cmd_walg_restore(
        plane, directory=str(tmp_path / "out"), backup="LATEST", time=None, lsn=None
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "restore failed: backup-fetch failed" in err
    assert "remove any partly restored content" in err


def test_restore_refuses_a_directory_that_holds_something(
    sandbox: Sandbox, plane: DataPlaneConfig, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    occupied = tmp_path / "out"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("mine")

    code = walg_cmd.cmd_walg_restore(
        plane, directory=str(occupied), backup="LATEST", time=None, lsn=None
    )

    assert code == 1
    assert "exists and is not an empty directory" in capsys.readouterr().err
    assert (occupied / "keep.txt").read_text() == "mine"
    assert not sandbox.calls()


def test_restore_while_wal_g_is_off_says_so(
    plane_off: DataPlaneConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    code = walg_cmd.cmd_walg_restore(
        plane_off, directory="/x", backup="LATEST", time=None, lsn=None
    )

    assert code == 1
    assert "WAL-G is off" in capsys.readouterr().out


def test_drill_prints_timestamped_lines_and_returns_the_drills_exit_code(
    sandbox: Sandbox,
    capsys: pytest.CaptureFixture[str],
    command_runtime: CommandRuntime,
    operator_database: OperatorDatabaseFactory,
) -> None:
    code = _at_the_door(
        ["backup", "walg", "drill"],
        command_runtime,
        operator_database,
        walg_config_file=sandbox.config_file,
    )

    assert code == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines
    assert all(_STAMPED.match(line) for line in lines)


def test_status_shows_the_last_drill_and_the_last_success(
    plane: DataPlaneConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    stamp = datetime(2026, 10, 2, 6, 25, tzinfo=UTC)
    state.write_state(
        state.State(
            tick=state.TickRecord(started_at=stamp),
            drill=state.DrillRecord(
                finished_at=stamp,
                ok=False,
                backup="base_000000010000000000000087",
                target_lsn="0/A3000000",
                seconds=612.4,
                detail="recovery failed: no segment",
                last_ok_at=None,
            ),
        )
    )

    assert walg_cmd.cmd_walg_status(plane) == 0

    out = capsys.readouterr().out
    assert (
        "last drill: FAILED (2026-10-02T06:25:00+00:00): base_000000010000000000000087 to 0/A3000000 in 612s; recovery failed: no segment"
        in out
    )
    assert "last successful drill: never" in out
