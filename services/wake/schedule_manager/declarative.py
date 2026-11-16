# meiva:note
# Launch and settle declarative schedule fires without PTY sessions.

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeGuard

from psycopg import Connection
from psycopg_pool import ConnectionPool

from base.clock import Clock
from base.daemon.schedules.watcher import next_fire
from base.db.rows import query_all, query_one
from base.db.transaction import write_transaction
from base.deploy.schedule_timing import SCHEDULE_LAUNCH_GRACE_S
from base.host.atomic_io import write_text_atomic
from base.host.boundary.untyped import object_of
from base.native_process.process_control import ProcessControl
from services.wake.schedule_manager.run_custody import (
    ClaimReadState,
    GroupLiveness,
    LeaderLiveness,
    ProcessIdentityControl,
    RunClaim,
    capture_run_leader,
    clear_run_claim,
    kill_run_group,
    observe_run_group,
    read_run_claim,
    write_run_claim,
)

_log = logging.getLogger(__name__)

_OUTCOME_PREFIX = ".run-"


# meiva:note
# Limits and cluster facts read once by the schedule-manager composition root.
@dataclass(frozen=True)
class ManagerPolicy:
    auto_restart_max_per_window: int
    auto_restart_window_seconds: float
    recovery_gate_alert_seconds: float
    timezone: str

    def __post_init__(self) -> None:
        if not (
            self.auto_restart_max_per_window > 0
            and self.auto_restart_window_seconds > 0
            and self.recovery_gate_alert_seconds > 0
        ):
            raise ValueError("every schedule manager limit must be positive")


@dataclass(frozen=True)
class _DueRow:
    id: int
    script: str
    trigger_kind: str
    next_launch_at: datetime
    timeout_seconds: int
    owner_agent_id: int | None
    secret_env_vars: Sequence[str]


@dataclass(frozen=True)
class _OpenRunRow:
    run_id: int
    schedule_id: int
    ran_at: datetime
    scheduled_for: datetime
    trigger_kind: str
    cron_expr: str | None
    interval_seconds: int | None
    timezone: str
    timeout_seconds: int
    enabled: bool
    status: str
    owner_agent_id: int | None


@dataclass(frozen=True)
class _ClaimedFire:
    run_id: int
    schedule: _DueRow


@dataclass(frozen=True)
class _Outcome:
    ok: bool
    returncode: int
    timed_out: bool
    note: str | None

    @classmethod
    def read(cls, path: Path) -> _Outcome | None:
        try:
            raw = object_of(json.loads, path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not _is_dict(raw):
            return None
        ok = raw.get("ok")
        returncode = raw.get("returncode")
        timed_out = raw.get("timed_out")
        note = raw.get("note")
        if (
            not isinstance(ok, bool)
            or not _is_int(returncode)
            or not isinstance(timed_out, bool)
            or (note is not None and not isinstance(note, str))
        ):
            return None
        return cls(ok, returncode, timed_out, note)


# meiva:note
# Agent ownership selects workspace-local storage; system schedules use the cluster schedule root.
def schedule_directory(home: Path, owner_agent_id: int | None, schedule_id: int) -> Path:
    if owner_agent_id is None:
        return home / "schedules" / str(schedule_id)
    return home / "workspaces" / str(owner_agent_id) / "schedules" / str(schedule_id)


def _outcome_path(work_dir: Path, run_id: int) -> Path:
    return work_dir / f"{_OUTCOME_PREFIX}{run_id}.json"


def _release_launch_gate(write_fd: int) -> None:
    if os.write(write_fd, b"\x01") != 1:
        raise RuntimeError("single-round launch gate accepted no byte")


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


# meiva:note
# Own due-slot admission, non-PTY launch, recovery, and settlement.
class DeclarativeScheduleEngine:
    def __init__(
        self,
        pool: ConnectionPool,
        *,
        home: Path,
        repo_root: Path,
        machine_name: str,
        launch_env: Mapping[str, str],
        clock: Clock,
        process_control: ProcessControl,
        identity_control: ProcessIdentityControl,
        secret_env_resolver: Callable[[int, tuple[str, ...]], Mapping[str, str]] | None,
    ) -> None:
        self._pool = pool
        self._home = home
        self._repo_root = repo_root
        self._machine_name = machine_name
        self._launch_env = launch_env
        self._clock = clock
        self._process_control = process_control
        self._identity_control = identity_control
        self._secret_env_resolver = secret_env_resolver
        self._children: list[subprocess.Popen[bytes]] = []

    def reconcile(self) -> None:
        now = self._clock.now().astimezone(UTC)
        self._reap_children()
        for open_run in self._open_runs():
            self._observe_open_run(open_run, now)
        while claimed := self._claim_due(now):
            self._launch(claimed, now)

    def _reap_children(self) -> None:
        self._children = [child for child in self._children if child.poll() is None]

    def _open_runs(self) -> list[_OpenRunRow]:
        with self._pool.connection() as conn:
            return query_all(
                conn,
                _OpenRunRow,
                "SELECT r.id AS run_id, r.schedule_id, r.ran_at, r.scheduled_for, "
                "s.trigger_kind, s.cron_expr, s.interval_seconds, s.timezone, "
                "s.timeout_seconds, s.enabled, s.status, r.agent_id AS owner_agent_id "
                "FROM schedule_runs r JOIN schedules s ON s.id = r.schedule_id "
                "WHERE r.ok IS NULL AND s.trigger_kind <> 'daemon' AND s.machine = %s "
                "ORDER BY r.id",
                (self._machine_name,),
            )

    def _observe_open_run(self, run: _OpenRunRow, now: datetime) -> None:
        work_dir = schedule_directory(self._home, run.owner_agent_id, run.schedule_id)
        outcome_path = _outcome_path(work_dir, run.run_id)
        outcome = _Outcome.read(outcome_path)
        claim_reading = read_run_claim(work_dir, run.schedule_id, run.run_id)
        if claim_reading.state is not ClaimReadState.PRESENT:
            self._observe_claimless_run(
                run,
                outcome,
                claim_reading.state,
                claim_reading.detail,
                now,
            )
            return
        claim = claim_reading.claim
        if claim is None:
            raise RuntimeError("present run claim has no parsed value")
        self._observe_claimed_run(run, claim, outcome, outcome_path, work_dir, now)

    def _observe_claimless_run(
        self,
        run: _OpenRunRow,
        outcome: _Outcome | None,
        claim_state: ClaimReadState,
        detail: str,
        now: datetime,
    ) -> None:
        if claim_state is ClaimReadState.UNREADABLE:
            _log.warning(
                "schedule %s run %s custody is unprovable: %s",
                run.schedule_id,
                run.run_id,
                detail,
            )
            return
        if (now - run.ran_at).total_seconds() <= SCHEDULE_LAUNCH_GRACE_S:
            return
        self._settle_claimless_run(run, outcome, now)
        work_dir = schedule_directory(self._home, run.owner_agent_id, run.schedule_id)
        outcome_path = _outcome_path(work_dir, run.run_id)
        self._remove_run_files(work_dir, run.run_id, outcome_path)

    def _observe_claimed_run(
        self,
        run: _OpenRunRow,
        claim: RunClaim,
        outcome: _Outcome | None,
        outcome_path: Path,
        work_dir: Path,
        now: datetime,
    ) -> None:
        observed = observe_run_group(
            claim,
            process_control=self._process_control,
            identity_control=self._identity_control,
        )
        if observed.group is GroupLiveness.UNKNOWN:
            _log.warning(
                "schedule %s run %s process group is unprovable: %s",
                run.schedule_id,
                run.run_id,
                observed.detail,
            )
            return
        cancelled = not run.enabled or run.status == "cancelled"
        timed_out = outcome is None and now >= claim.deadline
        leader_lost = observed.leader is not LeaderLiveness.ALIVE
        if observed.group is GroupLiveness.PRESENT:
            if cancelled or timed_out or leader_lost:
                killed = kill_run_group(
                    claim,
                    process_control=self._process_control,
                    identity_control=self._identity_control,
                )
                if killed.group is GroupLiveness.UNKNOWN:
                    _log.warning(
                        "schedule %s run %s process group kill is unprovable: %s",
                        run.schedule_id,
                        run.run_id,
                        killed.detail,
                    )
            return
        if cancelled:
            self._close_cancelled_run(run.run_id)
        elif outcome is not None:
            self._settle(run, outcome, now)
        elif now >= claim.deadline:
            self._settle(run, self._timeout_outcome(run.timeout_seconds), now)
        else:
            self._settle(run, self._missing_outcome(), now)
        self._remove_run_files(work_dir, run.run_id, outcome_path)

    def _settle_claimless_run(
        self, run: _OpenRunRow, outcome: _Outcome | None, finished_at: datetime
    ) -> None:
        if not run.enabled or run.status == "cancelled":
            self._close_cancelled_run(run.run_id)
        elif outcome is not None:
            self._settle(run, outcome, finished_at)
        else:
            self._settle(run, self._missing_outcome(), finished_at)

    @staticmethod
    def _missing_outcome() -> _Outcome:
        return _Outcome(
            ok=False,
            returncode=1,
            timed_out=False,
            note="single-round process ended without an outcome",
        )

    @staticmethod
    def _timeout_outcome(timeout_seconds: int) -> _Outcome:
        return _Outcome(
            ok=False,
            returncode=124,
            timed_out=True,
            note=f"timed out after {timeout_seconds}s",
        )

    @staticmethod
    def _remove_run_files(work_dir: Path, run_id: int, outcome_path: Path) -> None:
        outcome_path.unlink(missing_ok=True)
        clear_run_claim(work_dir, run_id)

    def _claim_due(self, now: datetime) -> _ClaimedFire | None:
        with write_transaction(self._pool) as conn:
            due = query_one(
                conn,
                _DueRow,
                "SELECT s.id, s.script, s.trigger_kind, s.next_launch_at, s.timeout_seconds, "
                "s.owner_agent_id, s.secret_env_vars FROM schedules s "
                "WHERE s.machine = %s AND s.enabled AND s.trigger_kind <> 'daemon' "
                "AND s.next_launch_at <= %s "
                "AND NOT EXISTS (SELECT 1 FROM schedule_sync_requests q "
                "                WHERE q.schedule_id = s.id) "
                "AND NOT EXISTS (SELECT 1 FROM schedule_runs r "
                "                WHERE r.schedule_id = s.id AND r.ok IS NULL) "
                "ORDER BY s.next_launch_at, s.id FOR UPDATE OF s SKIP LOCKED LIMIT 1",
                (self._machine_name, now),
            )
            if due is None:
                return None
            row = conn.execute(
                "INSERT INTO schedule_runs (schedule_id, agent_id, scheduled_for) "
                "VALUES (%s, %s, %s) RETURNING id",
                (due.id, due.owner_agent_id, due.next_launch_at),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"schedule {due.id} run admission returned no id")
            run_id = object_of(row.__getitem__, 0)
            if not _is_int(run_id):
                raise RuntimeError(f"schedule {due.id} run admission returned an invalid id")
            conn.execute(
                "UPDATE schedules SET status = 'running', last_error = NULL, updated_at = now() "
                "WHERE id = %s",
                (due.id,),
            )
            return _ClaimedFire(run_id, due)

    def _launch(self, claimed: _ClaimedFire, now: datetime) -> None:
        schedule = claimed.schedule
        work_dir = schedule_directory(self._home, schedule.owner_agent_id, schedule.id)
        work_dir.mkdir(parents=True, exist_ok=True)
        write_text_atomic(work_dir / "run.py", schedule.script, sync_file=False)
        outcome_path = _outcome_path(work_dir, claimed.run_id)
        outcome_path.unlink(missing_ok=True)

        env = dict(self._launch_env)
        env["MEIVA_HOME"] = str(self._home)
        env["MEIVA_SCHEDULE_ID"] = str(schedule.id)
        env["MEIVA_SCHEDULED_FOR"] = schedule.next_launch_at.isoformat()
        if schedule.owner_agent_id is not None:
            env["MEIVA_AGENT_ID"] = str(schedule.owner_agent_id)
        self._merge_secrets(env, schedule.id, tuple(schedule.secret_env_vars))

        deadline = now + timedelta(seconds=schedule.timeout_seconds)
        read_fd, write_fd = os.pipe()
        command = [
            str(self._repo_root / ".venv" / "bin" / "python"),
            "-m",
            "services.wake.schedule_manager.runner",
            "--single-round",
            str(schedule.id),
            str(claimed.run_id),
            str(schedule.timeout_seconds),
            str(deadline.timestamp()),
            str(outcome_path),
            str(read_fd),
        ]
        child: subprocess.Popen[bytes] | None = None
        released = False
        try:
            with (work_dir / "latest.log").open("wb") as output:
                # meiva:note
                # The child cannot execute run.py until this parent durably records its native
                # process-group custody and releases the one-byte launch gate.
                child = subprocess.Popen(  # noqa: S603
                    command,
                    cwd=work_dir,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    pass_fds=(read_fd,),
                )
            os.close(read_fd)
            read_fd = -1
            leader = capture_run_leader(child.pid, process_control=self._process_control)
            _ = write_run_claim(
                work_dir,
                RunClaim(schedule.id, claimed.run_id, leader, leader.pid, deadline),
            )
            _release_launch_gate(write_fd)
            released = True
        except Exception as exc:
            _log.exception("schedule %s single-round launch failed", schedule.id)
            stopped = child is None or self._stop_gated_child(child)
            failure = _Outcome(
                ok=False,
                returncode=1,
                timed_out=False,
                note=f"launch failed: {type(exc).__name__}: {exc}",
            )
            if stopped:
                clear_run_claim(work_dir, claimed.run_id)
                open_run = self._open_run(claimed.run_id)
                if open_run is not None:
                    self._settle(open_run, failure, now)
            else:
                write_text_atomic(
                    outcome_path,
                    json.dumps(failure.__dict__, sort_keys=True) + "\n",
                    sync_file=False,
                )
            return
        finally:
            if read_fd >= 0:
                os.close(read_fd)
            os.close(write_fd)
        if not released:
            raise RuntimeError("single-round launch returned without releasing its child")
        self._children.append(child)
        _log.info(
            "schedule %s launched run %s without PTY (pid=%s)",
            schedule.id,
            claimed.run_id,
            child.pid,
        )

    @staticmethod
    def _stop_gated_child(child: subprocess.Popen[bytes]) -> bool:
        try:
            _ = child.kill()
            _ = child.wait(timeout=2)
        except ProcessLookupError:
            return child.poll() is not None
        except subprocess.TimeoutExpired:
            return False
        return True

    def _merge_secrets(self, env: dict[str, str], schedule_id: int, names: tuple[str, ...]) -> None:
        if not names or self._secret_env_resolver is None:
            return
        try:
            resolved = self._secret_env_resolver(schedule_id, names)
        except Exception as exc:
            _log.error("failed resolving launch secrets for schedule %s: %s", schedule_id, exc)
            return
        for name, value in resolved.items():
            if name in names and not name.startswith("MEIVA_"):
                env[name] = value

    def _open_run(self, run_id: int) -> _OpenRunRow | None:
        with self._pool.connection() as conn:
            return query_one(
                conn,
                _OpenRunRow,
                "SELECT r.id AS run_id, r.schedule_id, r.ran_at, r.scheduled_for, "
                "s.trigger_kind, s.cron_expr, s.interval_seconds, s.timezone, "
                "s.timeout_seconds, s.enabled, s.status, r.agent_id AS owner_agent_id "
                "FROM schedule_runs r JOIN schedules s ON s.id = r.schedule_id "
                "WHERE r.id = %s AND r.ok IS NULL",
                (run_id,),
            )

    def _settle(self, run: _OpenRunRow, outcome: _Outcome, finished_at: datetime) -> None:
        note = outcome.note
        if note is None and not outcome.ok:
            note = f"process exited {outcome.returncode}"
        with write_transaction(self._pool) as conn:
            updated = conn.execute(
                "UPDATE schedule_runs SET ok = %s, note = %s WHERE id = %s AND ok IS NULL",
                (outcome.ok, note, run.run_id),
            ).rowcount
            if not updated:
                return
            self._advance_schedule(conn, run, outcome, finished_at)

    def _advance_schedule(
        self,
        conn: Connection[object],
        run: _OpenRunRow,
        outcome: _Outcome,
        finished_at: datetime,
    ) -> None:
        if run.trigger_kind == "at":
            status = "completed" if outcome.ok else "error"
            conn.execute(
                "UPDATE schedules SET enabled = false, status = %s, next_launch_at = NULL, "
                "last_error = %s, updated_at = %s WHERE id = %s AND status <> 'cancelled'",
                (status, None if outcome.ok else outcome.note, finished_at, run.schedule_id),
            )
            return
        next_launch = self._next_recurring_launch(run, finished_at)
        conn.execute(
            "UPDATE schedules SET status = %s, next_launch_at = %s, last_error = %s, "
            "updated_at = %s WHERE id = %s AND enabled AND status <> 'cancelled'",
            (
                "scheduled" if outcome.ok else "error",
                next_launch,
                None if outcome.ok else outcome.note,
                finished_at,
                run.schedule_id,
            ),
        )

    @staticmethod
    def _next_recurring_launch(run: _OpenRunRow, finished_at: datetime) -> datetime:
        if run.trigger_kind == "every":
            if run.interval_seconds is None:
                raise RuntimeError(f"every schedule {run.schedule_id} has no interval")
            return finished_at + timedelta(seconds=run.interval_seconds)
        if run.trigger_kind == "cron":
            if run.cron_expr is None:
                raise RuntimeError(f"cron schedule {run.schedule_id} has no expression")
            return next_fire(run.cron_expr, max(run.scheduled_for, finished_at), run.timezone)
        raise RuntimeError(f"unknown declarative trigger {run.trigger_kind!r}")

    def _close_cancelled_run(self, run_id: int) -> None:
        with write_transaction(self._pool) as conn:
            conn.execute(
                "UPDATE schedule_runs SET ok = false, note = 'cancelled' "
                "WHERE id = %s AND ok IS NULL",
                (run_id,),
            )
