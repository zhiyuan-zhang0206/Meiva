# meiva:note
# The daily WAL-G tick (`meiva backup walg run`): back up, verify the chain, apply retention.
#
# One command, run once a day by the OS job and by hand. It is safe to run at any
# moment and any number of times:
#
# 1. **Gates.** Key unset: nothing to do. Another tick holds the per-home lock: that run
#    is still going, nothing to do. Postgres does not accept connections: the tick is
#    *skipped* (recorded, exit 0) rather than failed, so a tick that happens to land
#    inside a stop/start window does not become a day-long alert. A skip leaves the outcome of the last real run untouched.
# 2. **Preflight.** The pinned binary, the configuration and key pin, and one
#    `backup-list`, which proves the credentials and the prefix.
# 3. **Recovery drill**, once a week (`drill.py`), before the backup, so that it restores
#    yesterday's backup: the one a lost host would need. A failed drill is recorded in
#    `drill` and never stops the backup.
# 4. **Backup.** `backup-push` with `WALG_DELTA_MAX_STEPS=6`: WAL-G itself starts a new
#    full backup once the increment chain is six deep (a daily run therefore makes a
#    full backup every seventh time), and retries a failed full backup the next day
#    because the chain is still full. A backup that WAL-G reports done but that is not
#    in the list afterwards is a failure.
# 5. **Verify.** `wal-verify integrity timeline --json`, JSON status only. A failure
#    here stops the tick before retention.
# 6. **Retention.** `retain FULL 3`, guarded (see `retention.py`).
#
# The first failing step ends the run and is recorded with its name; the next day's
# tick starts from scratch. Every step's result is written to `state.json`, which the
# health probe reads (`probe.py`). Output goes through `report`, one line at a time.

from __future__ import annotations

import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from base.cluster.dataplane import pg_admin, walg_binary
from base.cluster.dataplane.pg_throwaway_base import ThrowawayConfig
from base.db import Database
from base.log import logger
from base.native_process.os_platform import LockTimeoutError, file_lock
from base.paths.private_dir import ensure_private_dir
from services.backup.walg import config as walg_config
from services.backup.walg import drill, state
from services.backup.walg.backups import Backup, BackupChainError, list_backups
from services.backup.walg.pg_target import PgTarget, pg_target
from services.backup.walg.retention import RetentionAbortedError, apply_retention
from services.backup.walg.runner import WalgCommandError, run_walg
from services.backup.walg.state import (
    STEP_BACKUP,
    STEP_PREFLIGHT,
    STEP_RETENTION,
    STEP_VERIFY,
    BackupRecord,
    RetentionRecord,
    RunRecord,
    TickRecord,
    VerifyRecord,
)
from services.backup.walg.verify import VerifyOutputError, verify_chain

DELTA_MAX_STEPS = "6"
"""`WALG_DELTA_MAX_STEPS`: increments allowed before WAL-G takes a full backup (design
ruling: with a daily run, one full backup every seven runs)."""

BACKUP_TIMEOUT_S = 12 * 3600
"""Bound for `backup-push`: a base backup that has not finished in half a day is hung
(measured and extrapolated durations are well under two hours). A bound that keeps the
lock from being held forever, not an alert threshold."""

_CONNECT_TIMEOUT_S = 5

Report = Callable[[str], None]

# Failures whose message already says what went wrong; anything else is a bug and is
# recorded with its type as well.
_EXPECTED_FAILURES = (
    WalgCommandError,
    walg_config.WalgConfigError,
    BackupChainError,
    VerifyOutputError,
    RetentionAbortedError,
)


def postgres_accepts_connections(target: PgTarget) -> bool:
    """Whether the home's postmaster answers on its socket (and is the one that owns the data)."""
    try:
        with pg_admin.connect(
            target.admin_url,
            expected_data_dir=target.data_dir,
            autocommit=True,
            connect_timeout=_CONNECT_TIMEOUT_S,
        ):
            return True
    except psycopg.Error:
        return False


# meiva:note
# The facts about this machine one run is decided on.
#
# `binary_problem` is why the installed wal-g is not the pinned build (None when it is).
# `target` is the home's locally owned Postgres and `accepting` whether it answers on its
# socket. `setup_error` says why there is no usable target at all: no locally owned
# Postgres, or a socket that is not this home's Postgres.
@dataclass(frozen=True)
class Platform:
    binary_problem: str | None
    target: PgTarget | None = None
    accepting: bool = False
    setup_error: str | None = None

    def __post_init__(self) -> None:
        if self.target is None and self.setup_error is None:
            raise ValueError("a platform without a target must say why")


# meiva:note
# Resolve the platform from this process's home and settings (the composition root's read).
def ambient_platform() -> Platform:
    binary_problem = walg_binary.installed_problem()
    try:
        target = pg_target()
        accepting = postgres_accepts_connections(target)
    # meiva:note
    # no locally owned Postgres, or a socket that is not this home's
    except RuntimeError as exc:
        return Platform(binary_problem, setup_error=str(exc))
    return Platform(binary_problem, target=target, accepting=accepting)


class StepFailedError(Exception):
    """A step failed: its name (for the alert) and the detail (for the log)."""

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"{step}: {detail}")
        self.step = step
        self.detail = detail


def _now() -> datetime:
    return datetime.now(UTC)


def _preflight(*, path: Path | None, binary_problem: str | None) -> list[Backup]:
    if binary_problem is not None:
        raise StepFailedError(STEP_PREFLIGHT, f"{binary_problem} (meiva converge installs it)")
    try:
        walg_config.load_walg_config(path=path)
    except walg_config.WalgConfigError as exc:
        raise StepFailedError(STEP_PREFLIGHT, str(exc)) from None
    return list_backups(path=path)


def _backup(
    target: PgTarget,
    before: list[Backup],
    report: Report,
    *,
    path: Path | None,
) -> list[Backup]:
    run_walg(
        ["backup-push", str(target.data_dir)],
        timeout_s=BACKUP_TIMEOUT_S,
        pg_admin_url=target.admin_url,
        extra_env={"WALG_DELTA_MAX_STEPS": DELTA_MAX_STEPS},
        path=path,
    )
    after = list_backups(path=path)
    if not after or (before and after[-1].name == before[-1].name):
        raise StepFailedError(STEP_BACKUP, "backup-push finished but no new backup is listed")
    newest = after[-1]
    kind = "full" if newest.is_full else "delta"
    state.update_state(
        backup=BackupRecord(
            name=newest.name,
            kind=kind,
            finished_at=_now(),
            uncompressed_bytes=newest.uncompressed_bytes,
            compressed_bytes=newest.compressed_bytes,
        )
    )
    report(f"backup: {newest.name} ({kind}, {newest.compressed_bytes} bytes stored)")
    return after


def _drill(
    target: PgTarget,
    backups: list[Backup],
    report: Report,
    *,
    throwaway: ThrowawayConfig,
    database_for_url: Callable[[str], Database],
    path: Path | None,
) -> bool:
    """Drill the newest backup now; records the outcome. Returns whether it succeeded."""
    previous = state.read_state().drill
    record = drill.run_drill(
        target,
        backups[-1],
        previous,
        report,
        throwaway=throwaway,
        path=path,
        database_for_url=database_for_url,
    )
    state.update_state(drill=record)
    if record.ok:
        report(f"drill: ok in {record.seconds:.0f}s ({record.detail})")
    else:
        report(f"drill: FAILED after {record.seconds:.0f}s: {record.detail}")
    return record.ok


def _weekly_drill(
    target: PgTarget,
    backups: list[Backup],
    report: Report,
    *,
    throwaway: ThrowawayConfig,
    database_for_url: Callable[[str], Database],
    path: Path | None,
) -> None:
    """The drill when one is due. Its failure is recorded and reported, never raised."""
    if drill.drill_due(state.read_state().drill, backups, _now()):
        _drill(
            target,
            backups,
            report,
            path=path,
            database_for_url=database_for_url,
            throwaway=throwaway,
        )


def _verify(target: PgTarget, report: Report, *, path: Path | None) -> str:
    verdict = verify_chain(target.admin_url, path=path)
    state.update_state(
        verify=VerifyRecord(at=_now(), integrity=verdict.integrity, timeline=verdict.timeline)
    )
    summary = verdict.summary()
    report(f"verify: {summary}")
    if verdict.failed:
        raise StepFailedError(STEP_VERIFY, f"the archived WAL chain is broken: {summary}")
    return summary


def _retention(backups: list[Backup], report: Report, *, path: Path | None) -> int:
    outcome = apply_retention(backups, report, path=path)
    state.update_state(
        retention=RetentionRecord(at=_now(), marked=outcome.marked, deleted=outcome.deleted)
    )
    return outcome.deleted


def _run_steps(
    target: PgTarget,
    binary_problem: str | None,
    report: Report,
    *,
    throwaway: ThrowawayConfig,
    database_for_url: Callable[[str], Database],
    path: Path | None,
) -> str:
    """The steps in order; returns the one-line summary of a successful run."""
    current = STEP_PREFLIGHT
    try:
        before = _preflight(path=path, binary_problem=binary_problem)
        report(f"preflight: ok, {len(before)} backups listed")
        _weekly_drill(
            target,
            before,
            report,
            path=path,
            database_for_url=database_for_url,
            throwaway=throwaway,
        )
        current = STEP_BACKUP
        after = _backup(target, before, report, path=path)
        current = STEP_VERIFY
        chain = _verify(target, report, path=path)
        current = STEP_RETENTION
        deleted = _retention(after, report, path=path)
    except StepFailedError:
        raise
    except _EXPECTED_FAILURES as exc:
        raise StepFailedError(current, str(exc)) from None
    except Exception as exc:  # any other failure is still this step's, never a silent crash
        logger.error(f"[walg] tick step {current} failed: {traceback.format_exc()}")
        raise StepFailedError(current, f"{type(exc).__name__}: {exc}") from None
    return f"{after[-1].name}; chain {chain}; retention deleted {deleted}"


def _record_failure(started: datetime, failure: StepFailedError, report: Report) -> int:
    state.update_state(
        run=RunRecord(
            started_at=started,
            finished_at=_now(),
            status="failed",
            step=failure.step,
            detail=failure.detail,
        )
    )
    report(f"failed at {failure.step}: {failure.detail}")
    return 1


def _locked_tick(
    report: Report,
    platform: Platform,
    *,
    throwaway: ThrowawayConfig,
    database_for_url: Callable[[str], Database],
    path: Path | None,
) -> int:
    started = _now()
    try:
        state.read_state()
    except state.StateError as exc:
        report(f"failed: {exc}; remove {state.state_path()} to reset it")
        return 1

    target = platform.target
    if platform.setup_error is not None or target is None:
        failure = StepFailedError(STEP_PREFLIGHT, platform.setup_error or "no Postgres target")
        return _record_failure(started, failure, report)
    if not platform.accepting:
        reason = "postgres is not accepting connections"
        state.update_state(tick=TickRecord(started_at=started, skipped=reason))
        report(f"skipped: {reason}")
        return 0

    state.update_state(tick=TickRecord(started_at=started))
    try:
        summary = _run_steps(
            target,
            platform.binary_problem,
            report,
            path=path,
            database_for_url=database_for_url,
            throwaway=throwaway,
        )
    except StepFailedError as failure:
        return _record_failure(started, failure, report)
    state.update_state(
        run=RunRecord(
            started_at=started, finished_at=_now(), status="ok", step=None, detail=summary
        )
    )
    report(f"ok: {summary}")
    return 0


# meiva:note
# Run one tick on `platform`; the exit code is non-zero only when a step failed.
def run_tick_on(
    report: Report,
    *,
    platform: Platform,
    throwaway: ThrowawayConfig,
    database_for_url: Callable[[str], Database],
    path: Path | None,
) -> int:
    if path is None:
        report("WAL-G is off (MEIVA_WALG_CONFIG_FILE is not set); nothing to do")
        return 0
    ensure_private_dir(state.walg_dir())
    try:
        with file_lock(state.lock_path(), timeout_s=0):
            return _locked_tick(
                report,
                platform,
                path=path,
                database_for_url=database_for_url,
                throwaway=throwaway,
            )
    except LockTimeoutError:
        report("another WAL-G tick is still running; nothing to do")
        return 0


# meiva:note
# Run one tick on this machine; the exit code is non-zero only when a step failed.
def run_tick(
    report: Report,
    *,
    throwaway: ThrowawayConfig,
    database_for_url: Callable[[str], Database],
    path: Path | None,
) -> int:
    if path is None:
        report("WAL-G is off (MEIVA_WALG_CONFIG_FILE is not set); nothing to do")
        return 0
    return run_tick_on(
        report,
        platform=ambient_platform(),
        throwaway=throwaway,
        database_for_url=database_for_url,
        path=path,
    )


def _locked_drill(
    report: Report,
    platform: Platform,
    *,
    throwaway: ThrowawayConfig,
    database_for_url: Callable[[str], Database],
    path: Path | None,
) -> int:
    target = platform.target
    try:
        state.read_state()
        if platform.setup_error is not None or target is None:
            report(f"failed: {platform.setup_error}")
            return 1
        if not platform.accepting:
            report("failed: postgres is not accepting connections")
            return 1
        backups = _preflight(path=path, binary_problem=platform.binary_problem)
    except (state.StateError, RuntimeError, StepFailedError, *_EXPECTED_FAILURES) as exc:
        report(f"failed: {exc}")
        return 1
    if not backups:
        report("failed: no backup exists yet; run `meiva backup walg run` first")
        return 1
    return (
        0
        if _drill(
            target,
            backups,
            report,
            path=path,
            database_for_url=database_for_url,
            throwaway=throwaway,
        )
        else 1
    )


# meiva:note
# Run the recovery drill on the newest backup now, on `platform`.
#
# Takes the same lock as the tick, so the two never overlap. Exit 0 only when the drill passed.
def run_drill_now_on(
    report: Report,
    *,
    platform: Platform,
    throwaway: ThrowawayConfig,
    database_for_url: Callable[[str], Database],
    path: Path | None,
) -> int:
    if path is None:
        report("WAL-G is off (MEIVA_WALG_CONFIG_FILE is not set); nothing to drill")
        return 1
    ensure_private_dir(state.walg_dir())
    try:
        with file_lock(state.lock_path(), timeout_s=0):
            return _locked_drill(
                report,
                platform,
                path=path,
                database_for_url=database_for_url,
                throwaway=throwaway,
            )
    except LockTimeoutError:
        report("failed: a WAL-G tick or drill is still running")
        return 1


# meiva:note
# Run the recovery drill on the newest backup now (the weekly one runs inside the tick).
#
# Takes the same lock as the tick, so the two never overlap. Exit 0 only when the drill passed.
def run_drill_now(
    report: Report,
    *,
    throwaway: ThrowawayConfig,
    database_for_url: Callable[[str], Database],
    path: Path | None,
) -> int:
    if path is None:
        report("WAL-G is off (MEIVA_WALG_CONFIG_FILE is not set); nothing to drill")
        return 1
    return run_drill_now_on(
        report,
        platform=ambient_platform(),
        throwaway=throwaway,
        database_for_url=database_for_url,
        path=path,
    )
