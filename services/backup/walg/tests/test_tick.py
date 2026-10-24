"""The daily tick against a scripted wal-g: step order, first failure stops, skips are not failures.

The fake wal-g (`fake_walg.sh`) answers from files in the sandbox: a backup list that
grows when `backup-push` runs, a `wal-verify` report, and the log `delete` prints.
The gates that need a live cluster (deploy window, Postgres socket) are replaced by
two attributes of `tick`; everything else (config, key pin, lock, state file, argv and
environment handed to wal-g) is the production code.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from base.cluster.dataplane.pg_throwaway_base import ThrowawayConfig
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_settings
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.native_process.os_platform import file_lock
from services.backup.walg import state, tick
from services.backup.walg.pg_target import PgTarget
from services.backup.walg.state import RunRecord
from services.backup.walg.tests.support import (
    SECRETS,
    Sandbox,
    fixture_text,
    make_sandbox,
    postgres_instance,
    refused_target,
)

FULL_4 = "base_0000000100000006000000C0"
T0 = datetime(2026, 10, 2, 6, 25, 0, tzinfo=UTC)

PG_DATA = Path("/pg/data")
DATABASE = "meiva_main"
ADMIN_URL = "postgresql://tester@/postgres?host=/sockets/meiva-pg-home&port=5433"
TARGET = tick.PgTarget(ADMIN_URL, PG_DATA, DATABASE)


# meiva:note
# The machine facts a run is decided on: the pinned binary, and a Postgres that answers.
def _platform(**changes: Any) -> tick.Platform:
    return replace(tick.Platform(binary_problem=None, target=TARGET, accepting=True), **changes)


# meiva:note
# A drill that succeeded just now, so the weekly drill is not due.
def _seed_recent_drill() -> state.DrillRecord:
    finished = datetime.now(UTC)
    record = state.DrillRecord(
        finished_at=finished,
        ok=True,
        backup="base_seed",
        target_lsn=None,
        seconds=1.0,
        detail="seeded",
        last_ok_at=finished,
    )
    state.write_state(state.State(drill=record))
    return record


def _database_factory() -> Callable[[str], Database]:
    image = LoadedCommit.capture()
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="backup-test")

    def for_url(url: str) -> Database:
        return Database(replace(db_config_from_settings(), db_url=url), gate=gate)

    return for_url


def _backup_lists() -> tuple[str, str]:
    before = json.loads(fixture_text("backup-list.json"))
    newest = dict(before[-1])
    newest.update(backup_name=FULL_4, start_time="2026-10-08T06:25:00.5Z")
    return json.dumps(before), json.dumps([*before, newest])


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    box = make_sandbox(tmp_path, monkeypatch)
    before, after = _backup_lists()
    box.put("backups.json", before)
    box.put("backups-after.json", after)
    box.put("wal-verify.json", fixture_text("wal-verify-warning.json"))
    box.put("delete-dry.log", fixture_text("retention-dry-run.log"))
    box.put("delete-confirm.log", _confirm_log())
    state.walg_dir().mkdir(parents=True, exist_ok=True)
    _seed_recent_drill()
    return box


def _confirm_log() -> str:
    dry = fixture_text("retention-dry-run.log")
    lines = [line for line in dry.splitlines() if "Dry run:" not in line]
    return "\n".join([*lines, "INFO: Objects deleted successfully: count=21"]) + "\n"


def _run(sandbox: Sandbox, platform: tick.Platform | None = None) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = tick.run_tick_on(
        lines.append,
        platform=_platform() if platform is None else platform,
        path=sandbox.path,
        database_for_url=_database_factory(),
        throwaway=ThrowawayConfig(None),
    )
    return code, lines


def _verify_report(*, integrity: str) -> str:
    report: dict[str, Any] = json.loads(fixture_text("wal-verify-warning.json"))
    report["integrity"]["status"] = integrity
    return json.dumps(report)


def _assert_no_secret_anywhere(lines: list[str]) -> None:
    haystack = state.state_path().read_text() + "\n".join(lines)
    for secret in SECRETS:
        assert secret not in haystack


# ── the happy path ───────────────────────────────────────────────────────────


def test_a_run_goes_preflight_backup_verify_retention_in_that_order(sandbox: Sandbox) -> None:
    code, lines = _run(sandbox)

    assert code == 0
    assert sandbox.calls() == [
        "backup-list --detail --json",
        f"backup-push {PG_DATA}",
        "backup-list --detail --json",
        "wal-verify integrity timeline --json",
        "delete retain FULL 3 --use-sentinel-time",
        "delete retain FULL 3 --use-sentinel-time --confirm",
    ]
    assert lines[0] == "preflight: ok, 4 backups listed"
    stored = json.loads(fixture_text("backup-list.json"))[-1]["compressed_size"]
    assert lines[1] == f"backup: {FULL_4} (full, {stored} bytes stored)"
    assert lines[2] == "verify: integrity WARNING, timeline OK"
    assert lines[-1] == f"ok: {FULL_4}; chain integrity WARNING, timeline OK; retention deleted 21"


def test_backup_push_gets_the_chain_limit_and_the_owner_only_socket(sandbox: Sandbox) -> None:
    _run(sandbox)

    assert sandbox.env_log() == [
        "PGHOST=/sockets/meiva-pg-home PGPORT=5433 PGUSER=tester WALG_DELTA_MAX_STEPS=6"
    ]


def test_wal_verify_gets_the_owner_only_socket_like_backup_push(sandbox: Sandbox) -> None:
    """Without the connection variables WAL-G dials libpq's default socket and fails."""
    _run(sandbox)

    assert sandbox.verify_env_log() == ["PGHOST=/sockets/meiva-pg-home PGPORT=5433 PGUSER=tester"]


def test_every_step_leaves_its_record_in_the_state_file(sandbox: Sandbox) -> None:
    seeded = state.read_state().drill
    before = datetime.now(UTC).replace(microsecond=0)

    code, lines = _run(sandbox)

    after = datetime.now(UTC)
    recorded = state.read_state()
    assert code == 0
    assert recorded.tick is not None and recorded.tick.skipped is None
    run = recorded.run
    assert run is not None
    assert (run.status, run.step) == ("ok", None)
    assert before <= recorded.tick.started_at == run.started_at <= run.finished_at <= after
    assert run.detail == f"{FULL_4}; chain integrity WARNING, timeline OK; retention deleted 21"
    assert recorded.backup is not None
    assert (recorded.backup.name, recorded.backup.kind) == (FULL_4, "full")
    assert recorded.verify is not None
    assert (recorded.verify.integrity, recorded.verify.timeline) == ("WARNING", "OK")
    assert recorded.retention is not None
    assert (recorded.retention.marked, recorded.retention.deleted) == (21, 21)
    assert recorded.drill == seeded, "a drill that is not due leaves its record alone"
    _assert_no_secret_anywhere(lines)


def test_an_increment_is_recorded_as_a_delta(sandbox: Sandbox) -> None:
    before = json.loads(fixture_text("backup-list.json"))
    delta = dict(before[-1])
    delta.update(
        backup_name="base_0000000100000006000000C0_D_0000000100000004000000B6",
        start_time="2026-10-08T06:25:00Z",
    )
    sandbox.put("backups-after.json", json.dumps([*before, delta]))

    code, _ = _run(sandbox)

    assert code == 0
    backup = state.read_state().backup
    assert backup is not None and backup.kind == "delta"


def test_with_few_full_backups_retention_does_not_ask_wal_g_to_delete(sandbox: Sandbox) -> None:
    listed = json.loads(fixture_text("backup-list.json"))[:2]  # one full backup and its increment
    newest = dict(listed[-1])
    newest.update(
        backup_name="base_0000000100000006000000C0_D_0000000100000000000000A6",
        start_time="2026-10-08T06:25:00Z",
    )
    sandbox.put("backups.json", json.dumps(listed))
    sandbox.put("backups-after.json", json.dumps([*listed, newest]))

    code, lines = _run(sandbox)

    assert code == 0
    assert not [call for call in sandbox.calls() if call.startswith("delete")]
    assert "retention: 3 or fewer full backups, nothing expires yet" in lines
    retention = state.read_state().retention
    assert retention is not None and (retention.marked, retention.deleted) == (0, 0)


def test_a_verify_warning_does_not_stop_the_run(sandbox: Sandbox) -> None:
    code, _ = _run(sandbox)

    assert code == 0
    assert any(call.endswith("--confirm") for call in sandbox.calls())


# ── failures: the first failing step ends the run, named ─────────────────────


def _assert_failed_at(step: str) -> RunRecord:
    run = state.read_state().run
    assert run is not None
    assert (run.status, run.step) == ("failed", step)
    return run


def test_a_failing_preflight_stops_before_any_backup(sandbox: Sandbox) -> None:
    sandbox.fail("backup-list")

    code, lines = _run(sandbox)

    assert code == 1
    _assert_failed_at("preflight")
    assert not [call for call in sandbox.calls() if call.startswith("backup-push")]
    assert lines[-1].startswith("failed at preflight: wal-g backup-list failed (exit 1)")
    _assert_no_secret_anywhere(lines)


def test_a_key_that_is_not_the_pinned_one_fails_preflight(sandbox: Sandbox) -> None:
    from services.backup.walg import config as walg_config

    # meiva:note
    # pins the key
    walg_config.load_walg_config(path=sandbox.path)
    sandbox.key_file.write_text("cd" * 32 + "\n")

    code, _ = _run(sandbox)

    assert code == 1
    assert "not the key this home pinned" in _assert_failed_at("preflight").detail
    assert sandbox.calls() == []


def test_a_failing_backup_push_stops_before_verify(sandbox: Sandbox) -> None:
    sandbox.fail("backup-push")

    code, _ = _run(sandbox)

    assert code == 1
    _assert_failed_at("backup")
    assert not [call for call in sandbox.calls() if call.startswith("wal-verify")]
    assert state.read_state().backup is None


def test_a_backup_that_never_appears_in_the_list_is_a_failure(sandbox: Sandbox) -> None:
    (sandbox.store_dir / "backups-after.json").unlink()

    code, _ = _run(sandbox)

    assert code == 1
    assert "no new backup is listed" in _assert_failed_at("backup").detail


def test_the_first_ever_backup_is_a_backup_even_with_an_empty_before_list(
    sandbox: Sandbox,
) -> None:
    sandbox.put("backups.json", "[]")
    sandbox.put("backups-after.json", json.dumps(json.loads(fixture_text("backup-list.json"))[:1]))

    code, _ = _run(sandbox)

    assert code == 0
    assert state.read_state().backup is not None


def test_a_chain_failure_with_exit_code_zero_stops_before_retention(sandbox: Sandbox) -> None:
    sandbox.put("wal-verify.json", _verify_report(integrity="FAILURE"))
    sandbox.put("wal-verify.rc", "0")

    code, lines = _run(sandbox)

    assert code == 1
    _assert_failed_at("verify")
    assert not [call for call in sandbox.calls() if call.startswith("delete")]
    verify = state.read_state().verify
    assert verify is not None and verify.integrity == "FAILURE"
    assert "the archived WAL chain is broken" in lines[-1]
    assert state.read_state().backup is not None, "the backup that happened is still recorded"


def test_a_failing_verify_command_stops_before_retention(sandbox: Sandbox) -> None:
    sandbox.fail("wal-verify")

    code, _ = _run(sandbox)

    assert code == 1
    _assert_failed_at("verify")
    assert not [call for call in sandbox.calls() if call.startswith("delete")]


def test_a_retention_invariant_violation_never_confirms(sandbox: Sandbox) -> None:
    bad = fixture_text("retention-dry-run.log").replace("count=21", "count=22") + (
        f"INFO: Object marked for deletion: basebackups_005/{FULL_4}/metadata.json storage=default\n"
    )
    sandbox.put("delete-dry.log", bad)

    code, lines = _run(sandbox)

    assert code == 1
    run = _assert_failed_at("retention")
    assert "newest backup depends on" in run.detail
    assert sandbox.calls()[-1] == "delete retain FULL 3 --use-sentinel-time"
    assert state.read_state().retention is None
    assert any(line.startswith("  basebackups_005/") for line in lines), "the plan was audited"


# meiva:note
# An admin URL without its socket directory is a bug upstream, not a wal-g failure: the
# environment builder raises a `KeyError` inside the backup step.
def test_an_unexpected_error_is_still_recorded_against_its_step(sandbox: Sandbox) -> None:
    broken = PgTarget("postgresql://tester@/postgres", PG_DATA, DATABASE)

    code, _ = _run(sandbox, _platform(target=broken))

    assert code == 1
    assert _assert_failed_at("backup").detail == "KeyError: 'host'"


def test_a_failed_run_is_replaced_by_the_next_successful_one(sandbox: Sandbox) -> None:
    sandbox.fail("backup-push")
    _run(sandbox)
    sandbox.fail()

    code, _ = _run(sandbox)

    assert code == 0
    run = state.read_state().run
    assert run is not None and run.status == "ok"


# ── skips and no-ops are not failures ────────────────────────────────────────


def test_off_does_nothing_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    box = make_sandbox(tmp_path, monkeypatch, enabled=False)

    code, lines = _run(box)

    assert (code, box.calls()) == (0, [])
    assert not state.state_path().exists()
    assert "nothing to do" in lines[0]


def test_postgres_not_accepting_connections_skips(sandbox: Sandbox) -> None:
    sandbox.fail("backup-push")
    _run(sandbox)
    failed = state.read_state().run
    calls_before = sandbox.calls()
    before = datetime.now(UTC).replace(microsecond=0)

    code, _ = _run(sandbox, _platform(accepting=False))

    assert (code, sandbox.calls()) == (0, calls_before)
    after = state.read_state()
    assert after.tick is not None
    assert after.tick.skipped == "postgres is not accepting connections"
    assert before <= after.tick.started_at <= datetime.now(UTC)
    assert after.run == failed


def test_a_tick_that_holds_the_lock_makes_a_second_one_stand_down(sandbox: Sandbox) -> None:
    seeded = state.state_path().read_bytes()

    with file_lock(state.lock_path()):
        code, lines = _run(sandbox)

    assert (code, sandbox.calls()) == (0, [])
    assert lines == ["another WAL-G tick is still running; nothing to do"]
    assert state.state_path().read_bytes() == seeded


def test_the_lock_is_released_after_a_run(sandbox: Sandbox) -> None:
    _run(sandbox)
    sandbox.put("backups.json", _backup_lists()[0])

    code, _ = _run(sandbox)

    assert code == 0


# ── setup errors are recorded, not crashes ───────────────────────────────────


def test_a_corrupt_state_file_fails_the_tick_without_touching_the_bucket(sandbox: Sandbox) -> None:
    state.state_path().write_text("{broken")

    code, lines = _run(sandbox)

    assert (code, sandbox.calls()) == (1, [])
    assert "remove" in lines[0] and str(state.state_path()) in lines[0]


def test_no_locally_owned_postgres_is_a_recorded_setup_failure(sandbox: Sandbox) -> None:
    platform = tick.Platform(
        binary_problem=None,
        setup_error="a remote-managed data plane has no local owner authority",
    )

    code, _ = _run(sandbox, platform)

    assert (code, sandbox.calls()) == (1, [])
    assert "remote-managed" in _assert_failed_at("preflight").detail


def test_a_socket_that_is_not_this_homes_postgres_is_a_recorded_failure(
    sandbox: Sandbox, tmp_path: Path, postgres_access: None
) -> None:
    with postgres_instance() as pg:
        foreign = PgTarget(pg.target.admin_url, tmp_path / "another-home" / "pg", "postgres")
        with pytest.raises((RuntimeError, OSError)):
            tick.postgres_accepts_connections(foreign)


# ── the Postgres gate ────────────────────────────────────────────────────────


def test_postgres_reachability_is_a_custody_checked_dial_of_the_admin_socket(
    postgres_access: None,
) -> None:
    with postgres_instance() as pg:
        assert tick.postgres_accepts_connections(pg.target) is True


def test_a_refused_connection_is_not_reachable(postgres_access: None) -> None:
    assert tick.postgres_accepts_connections(refused_target()) is False


def test_the_records_are_stamped_in_the_order_the_steps_ran(sandbox: Sandbox) -> None:
    before = datetime.now(UTC).replace(microsecond=0)

    _run(sandbox)

    recorded = state.read_state()
    assert recorded.run is not None and recorded.backup is not None
    assert recorded.verify is not None and recorded.retention is not None
    assert before <= recorded.run.started_at <= recorded.backup.finished_at
    assert recorded.backup.finished_at <= recorded.verify.at <= recorded.retention.at
    assert recorded.retention.at <= recorded.run.finished_at <= datetime.now(UTC)


# ── the weekly recovery drill ────────────────────────────────────────────────


def _newest_listed() -> str:
    return str(json.loads(fixture_text("backup-list.json"))[-1]["backup_name"])


# meiva:note
# Forget the seeded drill: none has ever succeeded, so the weekly drill is due.
def _due(sandbox: Sandbox) -> None:
    state.write_state(state.State())


# meiva:note
# The source Postgres of this platform is not there, so the real drill fails fast: its
# record names the backup it chose, and the run goes on to take its own.
def test_a_due_drill_is_attempted_on_yesterdays_backup_before_todays_is_taken(
    sandbox: Sandbox, postgres_access: None
) -> None:
    _due(sandbox)

    code, lines = _run(sandbox)

    assert code == 0, "the drill's failure is its own alert, not the run's"
    recorded = state.read_state()
    assert recorded.drill is not None and not recorded.drill.ok
    assert recorded.drill.backup == _newest_listed()
    assert any(line.startswith("drill: FAILED after") for line in lines)
    calls = sandbox.calls()
    assert calls[0] == "backup-list --detail --json"
    assert f"backup-push {PG_DATA}" in calls
    assert calls.index(f"backup-push {PG_DATA}") > 0
    assert recorded.run is not None and recorded.run.status == "ok"


def test_a_drill_that_is_not_due_does_not_run(sandbox: Sandbox) -> None:
    seeded = state.read_state().drill

    code, _ = _run(sandbox)

    assert code == 0
    assert state.read_state().drill == seeded
    assert not [call for call in sandbox.calls() if call.startswith("backup-fetch")]


def _drill_now(sandbox: Sandbox, lines: list[str], platform: tick.Platform | None = None) -> int:
    return tick.run_drill_now_on(
        lines.append,
        platform=_platform() if platform is None else platform,
        path=sandbox.path,
        database_for_url=_database_factory(),
        throwaway=ThrowawayConfig(None),
    )


def test_drill_now_runs_the_drill_records_it_and_exits_non_zero_when_it_fails(
    sandbox: Sandbox, postgres_access: None
) -> None:
    assert _drill_now(sandbox, []) == 1

    drill_record = state.read_state().drill
    assert drill_record is not None and not drill_record.ok
    assert drill_record.backup == _newest_listed()
    assert not [call for call in sandbox.calls() if call.startswith("backup-push")]


def test_drill_now_refuses_without_a_backup(sandbox: Sandbox) -> None:
    sandbox.put("backups.json", "[]")
    lines: list[str] = []

    assert _drill_now(sandbox, lines) == 1
    assert lines == ["failed: no backup exists yet; run `meiva backup walg run` first"]


def test_drill_now_needs_postgres_and_the_run_lock(sandbox: Sandbox) -> None:
    lines: list[str] = []
    assert _drill_now(sandbox, lines, _platform(accepting=False)) == 1
    assert lines == ["failed: postgres is not accepting connections"]

    with file_lock(state.lock_path(), timeout_s=0):
        assert _drill_now(sandbox, lines) == 1
    assert lines[-1] == "failed: a WAL-G tick or drill is still running"


def test_drill_now_while_wal_g_is_off_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = make_sandbox(tmp_path, monkeypatch, enabled=False)
    lines: list[str] = []

    assert _drill_now(sandbox, lines) == 1
    assert "WAL-G is off" in lines[0]
