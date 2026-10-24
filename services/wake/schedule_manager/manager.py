"""Admit one schedule session per explicit desired revision.

The manager adopts matching live PTYs, reaps obsolete sessions through the
identity-checked backend and consumes schedule_sync_requests. It commits a
one-attempt launch latch before creating the process. Lost or ambiguous starts
are errors requiring explicit start/restart; no crash retry or breaker remains.
The latch and the two-hour absence alert clock survive service restart.
Run-history rows left open by a dead process are closed when the manager next
launches or reaps that schedule's session, not by a periodic sweep.

Convergence that keeps failing is bounded: a reap that survives, or a live
session whose launch provenance cannot be read, is retried for at most
``BLOCKED_GIVE_UP_AFTER_S`` of one failure streak. The manager then stops,
records the schedule as an error, emits ``schedule_convergence_abandoned`` and
leaves the process alone until an explicit new revision (start, stop, restart,
edit, delete) authorizes another attempt.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, assert_never

from psycopg_pool import ConnectionPool

from base import telemetry
from base.cluster import session_name
from base.db.transaction import write_transaction
from base.deploy.schedule_timing import SCHEDULE_STALL_ALERT_AFTER_S
from base.sessions.protocol import SessionBackend
from services.wake.schedule_manager import host_restart, revisions
from services.wake.schedule_manager.blocked_streak import (
    ReapLog,
    Streak,
    give_up_message,
    host_unknown_due,
    note_blocked,
    reap_log_step,
)
from services.wake.schedule_manager.convergence import (
    Desired,
    Prepare,
    Reclaim,
    SyncAction,
    launch_command,
    plan_sync,
    prepare_verdict,
    reclaim_verdict,
)
from services.wake.schedule_manager.launch_events import emit_gate_held, emit_launch_refused
from services.wake.schedule_manager.manager_policy import ManagerPolicy
from services.wake.schedule_manager.recovery_gate import GateState, observe_gate, refuse

_log = logging.getLogger(__name__)

# Reconcile cadence. Snappier than cron (a schedule is a resident process, not a
# wall-clock fire) — a few seconds of detection
# lag on a crash is fine and keeps the polling cheap.
POLL_INTERVAL_S = 5.0

STALL_ALERT_AFTER_S = SCHEDULE_STALL_ALERT_AFTER_S

# meiva:link services/wake/schedule_manager/docs/host-replacement.meiva.okf.md
INTERRUPTED_HOST_REPLACED = "interrupted (session host replaced)"

# Repo root (services/wake/schedule_manager/../../.. == <repo>/) — the cwd the runner launches from, so its
# relative `.venv/bin/python` resolves into this checkout's venv.
REPO_ROOT = Path(__file__).resolve().parents[3]


# meiva:note
# The reconcile logic for the ``schedules`` table. One per cluster, owned by
# the ``schedule-manager`` service, whose loops call `reconcile` and
# `sync_one`. Launch admission and the stall-alert clock are columns of the
# schedule row; schedule sessions are never touched when the service stops
# (they survive it).
#
# The clock is a value: each tick the loop passes ``now`` (a monotonic reading), and the
# streak, log-cooldown and gate decisions are the pure functions of ``blocked_streak``,
# ``recovery_gate`` and ``convergence``. ``policy``, ``home`` and ``launch_env`` are read once
# by the composition root; the manager never reads settings or the environment.
class ScheduleManager:
    def __init__(
        self,
        db_pool: ConnectionPool[Any],
        *,
        backend: SessionBackend,
        repo_root: Path,
        home: Path,
        policy: ManagerPolicy,
        launch_env: Mapping[str, str],
    ) -> None:
        self._pool = db_pool
        self._backend = backend
        self._repo_root = repo_root
        self._home = home
        self._policy = policy
        self._launch_env = launch_env
        # Enabled sessions whose stale same-name survivor resisted a reap. A
        # live session normally means adopt, but these need another official
        # reap before their replacement may launch. In memory: a restarted
        # service meets the same survivor again (a session of another generation
        # is reaped by `_live_ids` itself).
        self._reap_retries: set[int] = set()
        # A D-state survivor can resist each five-second reconcile forever; log
        # the first failure and periodic summaries rather than flooding ERROR.
        self._reap_failure_logs: dict[int, ReapLog] = {}
        # meiva:note
        # The current streak of failed convergence attempts of each schedule.
        self._blocked: dict[int, Streak] = {}
        # schedule id -> desired revision at the moment the manager gave up. The
        # process is left alone until the desired revision changes (an explicit
        # start, stop, restart, edit or delete); in memory, so a restarted service
        # starts one new bounded streak.
        self._abandoned: dict[int, int | None] = {}
        # Serializes the reconcile tick against the sync requests, which run on
        # separate threads and both touch the backend and launch-admission columns.
        self._lock = threading.Lock()
        self._gate = GateState()
        self._host_generation: str | None = None
        self._host_restarts = host_restart.AutoRestartLedger()
        self._host_generation_unknown_logged = -math.inf

    # ── control (the sync-request consumer calls this) ──────────────────────

    # meiva:note
    # Converge the current durable desired revision, preserving an applied run.
    #
    # Return False while the attempt is blocked (a surviving
    # session, missing safe session provenance); the durable request stays
    # queued for the next try. A blocked streak is bounded: once the manager
    # gives up it returns True, so the request is consumed and not retried.
    def sync_one(self, schedule_id: int, *, now: float) -> bool:
        with self._lock:
            self._host_generation = self._read_host_generation()
            converged = self._sync_revision(schedule_id, now)
            if converged:
                self._blocked.pop(schedule_id, None)
            return converged or schedule_id in self._abandoned

    def _sync_revision(self, schedule_id: int, now: float) -> bool:
        if self._is_abandoned(schedule_id):
            return False
        with revisions.claim_convergence(self._pool, schedule_id) as owned:
            if not owned:
                return False
            return self._sync_owned_revision(schedule_id, now)

    def _sync_owned_revision(self, schedule_id: int, now: float) -> bool:
        wanted = revisions.desired(self._pool, schedule_id)
        live = (
            wanted is not None
            and wanted.enabled
            and schedule_id in self._live_ids(now, owned_id=schedule_id)
        )
        observed = revisions.launched_revision(self._home, schedule_id) if live else None
        action = plan_sync(wanted, live=live, observed=observed)
        if action is SyncAction.REAP_UNWANTED:
            return self._reap_unwanted(schedule_id, wanted, now)
        # meiva:note
        # every other action is for an enabled schedule.
        assert wanted is not None  # noqa: S101
        return self._apply_enabled(action, schedule_id, wanted, now)

    def _reap_unwanted(self, schedule_id: int, wanted: Desired | None, now: float) -> bool:
        if not self._reap(schedule_id, now, owned=True):
            return False
        if wanted is not None:
            revisions.mark_applied(self._pool, schedule_id, wanted.revision)
        return True

    def _apply_enabled(
        self, action: SyncAction, schedule_id: int, wanted: Desired, now: float
    ) -> bool:
        match action:
            case SyncAction.ADOPT_LEGACY | SyncAction.SETTLED:
                return True
            case SyncAction.HOLD_UNREADABLE:
                self._note_blocked(
                    schedule_id, "launch provenance of the live session is unreadable", now
                )
                return False
            case SyncAction.ADOPT_CURRENT:
                self._mark_running(schedule_id, wanted.revision, self._host_generation)
                return True
            case SyncAction.REPLACE_STALE:
                if not self._reap(schedule_id, now, owned=True):
                    self._reap_retries.add(schedule_id)
                    return False
            case SyncAction.RECLAIM_RUNNING:
                if not self._reclaim_after_host_replacement(schedule_id, wanted, now):
                    self._mark_lost(schedule_id, wanted.revision)
                    return True
            case SyncAction.LAUNCH:
                pass
            case SyncAction.REAP_UNWANTED:
                raise ValueError("a reap is applied before an enabled launch is planned")
            case _:
                assert_never(action)
        return self._launch_revision(schedule_id, now)

    def _launch_revision(self, schedule_id: int, now: float) -> bool:
        from base.deploy.lifecycle import start_serving

        with start_serving.recovery_permitted(self._home) as permitted:
            if not permitted:
                self._refuse_launch(
                    schedule_id, "the serving receipt does not name this root; run meiva start"
                )
                return False
            self._launch(schedule_id, now)
        latest = revisions.desired(self._pool, schedule_id)
        if latest is None or latest.revision != latest.applied:
            self._refuse_launch(schedule_id, "the launch did not record the desired revision")
            return False
        return True

    def _refuse_launch(self, schedule_id: int, reason: str) -> None:
        self._gate, first = refuse(self._gate, schedule_id)
        if first:
            emit_launch_refused(schedule_id, reason)

    # ── reconcile (blocking: session backend + DB, run via asyncio.to_thread) ──

    def reconcile(self, *, now: float) -> None:
        with self._lock:
            self._reconcile_locked(now)

    def _reconcile_locked(self, now: float) -> None:
        self._host_generation = self._read_host_generation()
        if self._host_generation is None:
            self._note_host_generation_unknown(now)
            return
        # A given-up schedule is not retried: only a new revision lifts that.
        self._reap_retries.difference_update(self._abandoned)
        retrying = self._reap_retries.copy()
        # Read liveness before status: on a clean exit the runner commits
        # status='completed' before its process exits (and the session dies), so a
        # session seen dead here is guaranteed to already carry its terminal status.
        live = self._live_ids(now)  # {id}
        enabled_rows = self._load_enabled()
        enabled = set(enabled_rows)
        known = live | enabled
        self._forget_unknown(known)
        self._report_stalled_schedules(enabled_rows, live)
        # A launch can find a same-name session after liveness initially said it
        # was absent. If the official reap refused it, this set makes the next
        # reconcile retry rather than adopting the stale survivor forever.
        retrying.intersection_update(live)
        for sid in retrying:
            if self._retry_reap(sid, now):
                live.remove(sid)

        # Kill sessions we no longer want (disabled / deleted).
        orphans = live - enabled
        for sid in orphans:
            if sid not in retrying:
                self._sync_revision(sid, now)

        for sid in sorted(enabled):
            wanted = revisions.desired(self._pool, sid)
            if wanted is not None and (
                wanted.revision != wanted.applied or (sid in live and wanted.status == "error")
            ):
                self._sync_revision(sid, now)
        live = self._live_ids(now)
        enabled_rows = self._load_enabled()
        self._launch_missing(enabled_rows, sorted(set(enabled_rows) - live), now)
        self._observe_recovery_gate(now)

    def _observe_recovery_gate(self, now: float) -> None:
        from base.deploy.lifecycle import start_serving

        step = observe_gate(
            self._gate,
            permitted=start_serving.is_serving(self._home),
            now=now,
            alert_after_s=self._policy.recovery_gate_alert_seconds,
        )
        self._gate = step.state
        if step.held_for_s is not None:
            emit_gate_held(step.held_for_s)

    def _forget_unknown(self, known: set[int]) -> None:
        """Drop per-schedule retry state of schedules that are neither live nor enabled."""
        self._reap_retries.intersection_update(known)
        self._reap_failure_logs = {
            sid: state for sid, state in self._reap_failure_logs.items() if sid in known
        }
        self._blocked = {sid: span for sid, span in self._blocked.items() if sid in known}
        self._abandoned = {sid: rev for sid, rev in self._abandoned.items() if sid in known}
        self._host_restarts.prune(known)

    def _launch_missing(self, enabled_rows: dict[int, str], missing: list[int], now: float) -> None:
        """Apply explicit revisions; an admitted missing execution is never relaunched."""
        for sid in missing:
            if enabled_rows[sid] not in ("completed", "error"):
                self._sync_revision(sid, now)

    def _mark_lost(self, schedule_id: int, revision: int) -> None:
        with write_transaction(self._pool) as conn:
            changed = conn.execute(
                "UPDATE schedules SET status = 'error', last_error = %s, updated_at = now() "
                "WHERE id = %s AND desired_revision = %s AND applied_revision = %s "
                "AND enabled AND status = 'running'",
                (
                    "Schedule process lost; inspect effects and explicitly restart to run again",
                    schedule_id,
                    revision,
                    revision,
                ),
            ).rowcount
        if changed:
            _log.warning("schedule %s process lost; automatic relaunch disabled", schedule_id)

    # meiva:link services/wake/schedule_manager/docs/host-replacement.meiva.okf.md
    def _read_host_generation(self) -> str | None:
        return self._backend.host_generation()

    # meiva:note
    # While the session host does not answer, one reconcile pass takes no action: no session is declared lost, no schedule changes status, and no launch is attempted.
    # meiva:link services/wake/schedule_manager/docs/host-replacement.meiva.okf.md
    def _note_host_generation_unknown(self, now: float) -> None:
        if not host_unknown_due(self._host_generation_unknown_logged, now):
            return
        self._host_generation_unknown_logged = now
        _log.warning(
            "schedule reconcile skipped: the session host did not name its generation "
            "(an absent host means unknown, never an empty one)"
        )

    # meiva:link services/wake/schedule_manager/docs/host-replacement.meiva.okf.md
    def _reclaim_after_host_replacement(
        self, schedule_id: int, wanted: Desired, now: float
    ) -> bool:
        generation = self._host_generation
        launched_in = wanted.launch_generation
        window = self._policy.auto_restart_window_seconds
        intent = None if generation is None or launched_in is None else (launched_in, generation)
        replay = intent is not None and self._host_restarts.in_flight(schedule_id) == intent
        attempts = self._host_restarts.recent(schedule_id, window, now)
        verdict = reclaim_verdict(
            generation=generation,
            launched_in=launched_in,
            replay=replay,
            attempts=len(attempts),
            limit=self._policy.auto_restart_max_per_window,
        )
        if verdict is Reclaim.NOT_REPLACED:
            return False
        # meiva:note
        # The assert narrows a value the logic above guarantees.
        assert generation is not None and launched_in is not None and intent is not None  # noqa: S101
        if verdict is Reclaim.SUPPRESSED:
            host_restart.emit_auto_restart_suppressed(
                schedule_id, launched_in, generation, len(attempts), window
            )
            return False
        with write_transaction(self._pool) as conn:
            authorized = conn.execute(
                "UPDATE schedules SET launch_count = 0 WHERE id = %s AND enabled "
                "AND status = 'running' AND desired_revision = %s AND applied_revision = %s "
                "AND launch_generation = %s",
                (schedule_id, wanted.revision, wanted.applied, launched_in),
            ).rowcount
        if not authorized:
            return False
        self._close_null_runs(schedule_id, note=INTERRUPTED_HOST_REPLACED)
        if replay:
            return True
        self._host_restarts.record(schedule_id, intent, window, now)
        _log.warning(
            "schedule %s: session host %s was replaced by %s; relaunching once in the new generation",
            schedule_id,
            launched_in,
            generation,
        )
        return True

    def _report_stalled_schedules(self, status_by_id: dict[int, str], live_ids: set[int]) -> None:
        """Alert once when an active schedule has been sessionless for 2h.

        The clock is the row's ``not_live_since``; the alert claims
        ``stall_alerted_at`` in the same statement that finds it due, so one
        outage alerts once even across restarts. A live, completed or disabled
        schedule has both cleared, which rearms a later outage."""
        eligible = {sid for sid, status in status_by_id.items() if status != "completed"}
        missing = sorted(eligible - live_ids)
        with write_transaction(self._pool) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE schedules SET not_live_since = NULL, stall_alerted_at = NULL "
                "WHERE NOT (id = ANY(%s)) "
                "AND (not_live_since IS NOT NULL OR stall_alerted_at IS NOT NULL)",
                (missing,),
            )
            cur.execute(
                "UPDATE schedules SET not_live_since = clock_timestamp() "
                "WHERE id = ANY(%s) AND not_live_since IS NULL",
                (missing,),
            )
            cur.execute(
                "UPDATE schedules SET stall_alerted_at = clock_timestamp() "
                "WHERE id = ANY(%s) AND stall_alerted_at IS NULL "
                "AND not_live_since < clock_timestamp() - make_interval(secs => %s) "
                "RETURNING id, EXTRACT(EPOCH FROM clock_timestamp() - not_live_since)",
                (missing, STALL_ALERT_AFTER_S),
            )
            alerts = [(int(r[0]), float(r[1])) for r in cur.fetchall()]
        for schedule_id, stalled_seconds in alerts:
            rounded_seconds = round(stalled_seconds, 3)
            status = status_by_id[schedule_id]
            _log.warning(
                "schedule %s has had no live session for %.1fs (status=%s)",
                schedule_id,
                rounded_seconds,
                status,
            )
            telemetry.emit(
                "telemetry",
                event_name="schedule_stalled",
                level="warning",
                attributes={
                    "schedule_id": schedule_id,
                    "status": status,
                    "stalled_seconds": rounded_seconds,
                },
            )

    def _live_ids(self, now: float, *, owned_id: int | None = None) -> set[int]:
        from base.sessions.pty.allocation_freeze import current_generation

        live: set[int] = set()
        backend = self._backend
        generation = current_generation(home=self._home)
        prefix = session_name("schedule-")  # meiva-schedule-
        names = backend.list_sessions(prefix=prefix)
        _log.debug(
            "schedule session scan backend=%s settings_home=%s record_dir=%s names=%s",
            type(backend).__name__,
            self._home,
            self._home / "run" / "pty",
            names,
        )
        for name in names:
            tail = name.removeprefix(prefix)
            if tail.isdigit():
                schedule_id = int(tail)
                observed_generation = backend.session_generation(name)
                if observed_generation == generation:
                    live.add(schedule_id)
                    continue
                # An enabled schedule is current desired state, but a matching
                # name from the preceding flip is not its current exact
                # session. Reap the old record; an admitted revision stays
                # consumed, while an explicit new revision may launch.
                if owned_id is not None and owned_id != schedule_id:
                    live.add(schedule_id)
                    continue
                if schedule_id in self._reap_retries or schedule_id in self._abandoned:
                    live.add(schedule_id)
                    continue
                _log.warning(
                    "schedule %s: reaping superseded generation session %s",
                    tail,
                    name,
                )
                if not self._reap(
                    schedule_id,
                    now,
                    owned=owned_id == schedule_id,
                    expected_generation=observed_generation,
                ):
                    self._reap_retries.add(schedule_id)
                    # Do not launch a second runner while the old exact session
                    # is still live. The centralized reaper retries with
                    # rate-limited failure accounting on the next tick.
                    live.add(schedule_id)
        return live

    def _close_null_runs(self, schedule_id: int, *, note: str = "interrupted") -> None:
        """Close a schedule's in-progress run-history rows (ok IS NULL) as
        'interrupted' — the runner that opened them is gone. Best-effort (run
        history is severable observability) and idempotent (WHERE ok IS NULL):
        a row the runner closed itself is untouched. Called when the manager
        launches or reaps the schedule, the only moments it knows the process
        is gone; a process killed by something else leaves its row open until
        then."""
        with write_transaction(self._pool) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE schedule_runs SET ok = false, note = %s "
                "WHERE schedule_id = %s AND ok IS NULL",
                (note, schedule_id),
            )

    def _launch(self, schedule_id: int, now: float) -> None:
        wanted = revisions.desired(self._pool, schedule_id)
        if wanted is None or not wanted.enabled:
            return
        name = session_name(f"schedule-{schedule_id}")
        backend = self._backend
        # A gateway restart (rollout / crash) can leave the previous run's
        # session behind — the old runner process keeps serving until its PTY
        # host reaps it. The PTY backend's new_session is idempotent
        # (a live same-name session is left untouched), but a stale record or
        # a still-serving runner must not shadow the launch, so reap a
        # same-name survivor first (#1119, 2026-08-09 rollout). _launch is only
        # ever reached for a schedule the manager does not consider live (the
        # reconcile loop adopts live sessions without relaunching), so a
        # same-name survivor is by definition stale.
        if not self._prepare_launch(schedule_id, name, backend, wanted, now):
            return
        if not self._claim_launch(schedule_id, wanted.revision, self._host_generation):
            return
        # _launch is only reached for a schedule with no live session, so any
        # in-progress run row at this point belongs to a dead process (the
        # reaped survivor, an externally killed runner, a crash that skipped
        # its own close). Close those rows now — the new process opens a fresh
        # row, and without this the old one would render as in-progress beside
        # it forever (QA P2-2).
        self._close_null_runs(schedule_id)
        # The unit's config + this run's schedule id ride the env dict, which the
        # backend sends in the request body of the pty-sessions service's 0600
        # socket (never argv — issue #974). The service submits the command once
        # the login shell is ready.
        env = dict(self._launch_env)
        env["MEIVA_SCHEDULE_ID"] = str(schedule_id)
        # The runner's cron math is timezone-DEFINED: it must fire on the
        # CLUSTER's clock, not on whatever ambient env the session happens to
        # meiva:note
        # carry. The launch env deliberately excludes cluster-scope keys (a
        # child is expected to re-source .env at boot), but a unit .env that
        # misses MEIVA_TIMEZONE silently left the runner on the field default
        # America/Los_Angeles — 2026-08-21: schedule #3 fired at PT midnight
        # instead of Shanghai midnight after the 08-12 timezone ruling. Pin the
        # gateway's own resolved timezone into the spawn env so the fire time is
        # deterministic; dotenv_boot's authority pass still lets a declared .env
        # value override it (and never drops it — base/dotenv_boot._force_also).
        env["MEIVA_TIMEZONE"] = self._policy.timezone
        cmd = launch_command(str(self._repo_root), schedule_id, wanted.revision)
        # No shell-TTL row is written here (deliberate exemption, task
        # #2614): agent_shell_ttls rows key on an agent id, and schedule
        # sessions have none (meiva-schedule-<id> is allocated outside the
        # per-agent session counter). The ScheduleManager is their reclaimer
        # — reconcile reaps disabled/deleted schedules — and a registered 24h
        # deadline would make the TTL reaper kill a resident schedule every
        # day, one kill/relaunch cycle per schedule.
        try:
            backend.new_session(name, cmd, self._repo_root, env=env)
        except Exception as exc:
            # Admission was committed before the request. Its unknown outcome
            # consumes this revision; only an explicit restart may try again.
            _log.error("schedule %s launch failed: %s", schedule_id, exc)
            return
        if not backend.has_session(name):
            _log.error(
                "schedule %s launch failed: session %s did not come up "
                "(daemon down?) — explicit restart required",
                schedule_id,
                name,
            )
            return
        self._reap_retries.discard(schedule_id)
        _log.info("schedule %s launched (session %s)", schedule_id, name)
        intent = self._host_restarts.clear(schedule_id)
        self._mark_running(schedule_id, wanted.revision, self._host_generation)
        if intent is not None:
            host_restart.emit_auto_restart(
                schedule_id,
                intent[0],
                intent[1],
                len(
                    self._host_restarts.recent(
                        schedule_id, self._policy.auto_restart_window_seconds, now
                    )
                ),
            )

    def _mark_running(self, schedule_id: int, revision: int, epoch: str | None) -> None:
        """Confirm a matching live process without overwriting completion or newer intent."""
        with write_transaction(self._pool) as conn:
            conn.execute(
                "UPDATE schedules SET applied_revision = %s, "
                "launch_generation = COALESCE(%s, launch_generation), status = CASE "
                "WHEN applied_revision = %s AND status = 'completed' THEN status ELSE 'running' END, "
                "last_error = NULL WHERE id = %s AND desired_revision = %s AND enabled",
                (revision, epoch, revision, schedule_id, revision),
            )

    def _prepare_launch(
        self, schedule_id: int, name: str, backend: SessionBackend, wanted: Desired, now: float
    ) -> bool:
        from base.sessions.pty.allocation_freeze import current_generation

        present = name in backend.list_sessions()
        current = present and backend.session_generation(name) == current_generation(
            home=self._home
        )
        observed = revisions.launched_revision(self._home, schedule_id) if current else None
        match prepare_verdict(
            wanted, present=present, current_generation=current, observed=observed
        ):
            case Prepare.CLEAR:
                return True
            case Prepare.ADOPT:
                self._mark_running(schedule_id, wanted.revision, self._host_generation)
                return False
            case Prepare.HOLD:
                return False
            case Prepare.REAP:
                _log.warning("schedule %s: reaping stale session %s", schedule_id, name)
                if not self._reap(schedule_id, now, owned=True):
                    self._reap_retries.add(schedule_id)
                    return False
                return True

    def _is_abandoned(self, schedule_id: int) -> bool:
        """Whether the manager gave up on this schedule at its current revision.

        A changed desired revision (or a deleted row) is a new explicit request:
        it clears the give-up and authorizes a fresh bounded streak."""
        if schedule_id not in self._abandoned:
            return False
        wanted = revisions.desired(self._pool, schedule_id)
        if (None if wanted is None else wanted.revision) != self._abandoned[schedule_id]:
            del self._abandoned[schedule_id]
            self._blocked.pop(schedule_id, None)
            return False
        return True

    def _note_blocked(self, schedule_id: int, why: str, now: float) -> None:
        """Count one failed convergence attempt; give up when the streak is old enough."""
        streak, blocked_s = note_blocked(self._blocked.get(schedule_id), now)
        self._blocked[schedule_id] = streak
        if blocked_s is not None:
            self._give_up(schedule_id, why, blocked_s)

    def _give_up(self, schedule_id: int, why: str, blocked_s: float) -> None:
        """Stop retrying: record the schedule as an error, alert, leave the process alone."""
        wanted = revisions.desired(self._pool, schedule_id)
        self._abandoned[schedule_id] = None if wanted is None else wanted.revision
        self._reap_retries.discard(schedule_id)
        self._blocked.pop(schedule_id, None)
        message = give_up_message(why, blocked_s)
        with write_transaction(self._pool) as conn:
            conn.execute(
                "UPDATE schedules SET status = 'error', last_error = %s, updated_at = now() "
                "WHERE id = %s",
                (message, schedule_id),
            )
        _log.error("schedule %s: %s", schedule_id, message)
        telemetry.emit(
            "telemetry",
            event_name="schedule_convergence_abandoned",
            level="warning",
            attributes={
                "schedule_id": schedule_id,
                "reason": why,
                "blocked_seconds": round(blocked_s, 3),
            },
        )

    def _log_reap_failure(
        self,
        schedule_id: int,
        name: str,
        failure: str,
        now: float,
        *,
        exc: BaseException | None = None,
    ) -> None:
        """Log an unresolved reap at most once per schedule per cooldown."""
        state, held_back = reap_log_step(self._reap_failure_logs.get(schedule_id), now)
        self._reap_failure_logs[schedule_id] = state
        if held_back is None:
            return
        summary = "" if held_back == 0 else f" (suppressed {held_back} repeats)"
        _log.error(
            "schedule %s: PTY reap %s for session %s%s",
            schedule_id,
            failure,
            name,
            summary,
            exc_info=exc,
        )

    def _retry_reap(self, schedule_id: int, now: float) -> bool:
        with revisions.claim_convergence(self._pool, schedule_id) as owned:
            if not owned:
                return False
            wanted = revisions.desired(self._pool, schedule_id)
            observed = revisions.launched_revision(self._home, schedule_id)
            if wanted is not None and observed == wanted.revision == wanted.applied:
                self._reap_retries.discard(schedule_id)
                return False  # a different owner already installed this revision
            return self._reap(schedule_id, now, owned=True)

    def _reap(
        self,
        schedule_id: int,
        now: float,
        *,
        owned: bool = False,
        expected_generation: str | None = None,
    ) -> bool:
        if owned:
            return self._reap_owned(schedule_id, now, expected_generation)
        with revisions.claim_convergence(self._pool, schedule_id) as claimed:
            if not claimed:
                return False
            return self._reap_owned(schedule_id, now, expected_generation)

    def _reap_owned(
        self, schedule_id: int, now: float, expected_generation: str | None = None
    ) -> bool:
        """Reap one schedule PTY through its identity-checked backend.

        The backend's success result means the session is confirmed gone. Do
        not record a stopped schedule or close its in-progress run before that
        confirmation: disabled rows with a surviving PTY remain in the
        reconcile set and receive another official reap attempt next tick.
        """
        name = session_name(f"schedule-{schedule_id}")
        if self._is_abandoned(schedule_id):
            return False
        if (
            expected_generation is not None
            and self._backend.session_generation(name) != expected_generation
        ):
            return False
        try:
            reaped, mode = self._backend.kill_session(name)
        except Exception as exc:
            self._log_reap_failure(schedule_id, name, "raised", now, exc=exc)
            self._note_blocked(schedule_id, f"the session {name} could not be reaped", now)
            return False
        if not reaped:
            # SessionBackend's false verdict means the session survived its
            # own identity-aware kill. Keep metadata truthful until the next
            # bounded retry of the same official path.
            self._set_status(schedule_id, "running")
            self._log_reap_failure(schedule_id, name, "survived", now)
            self._note_blocked(schedule_id, f"the session {name} survived its reap", now)
            return False
        self._reap_retries.discard(schedule_id)
        self._reap_failure_logs.pop(schedule_id, None)
        self._blocked.pop(schedule_id, None)
        self._set_status(schedule_id, "stopped")
        # The killed process can never close its own run rows (the kill chain
        # is SIGKILL — no handler runs); close them now so a stop/restart/
        # edited-script save does not leave an in-progress "…" row behind (QA
        # P2-2). A kill that does not route through here leaves its row open
        # until the manager next launches or reaps this schedule.
        self._close_null_runs(schedule_id)
        _log.info("schedule %s: PTY session %s reaped (%s)", schedule_id, name, mode)
        return True

    # ── DB ──────────────────────────────────────────────────────────────────

    def _load_enabled(self) -> dict[int, str]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT id, status FROM schedules WHERE enabled").fetchall()
        return {row[0]: row[1] for row in rows}

    def _claim_launch(self, schedule_id: int, revision: int, epoch: str | None) -> bool:
        """Admit one process attempt before effects; ambiguity consumes the attempt.

        Existing launch_count is a one-attempt latch, reset only by a new
        explicit revision. Old retry timestamps are retained but never read.
        """
        with write_transaction(self._pool) as conn:
            return (
                conn.execute(
                    "UPDATE schedules SET launch_count = 1, applied_revision = %s, "
                    "launch_generation = %s, status = 'error', last_error = %s WHERE id = %s "
                    "AND enabled AND desired_revision = %s AND launch_count = 0",
                    (
                        revision,
                        epoch,
                        "Launch outcome unknown; inspect before explicitly restarting",
                        schedule_id,
                        revision,
                    ),
                ).rowcount
                == 1
            )

    def _set_status(self, schedule_id: int, status: str) -> bool:
        with write_transaction(self._pool) as conn:
            return (
                conn.execute(
                    "UPDATE schedules SET status=%s, updated_at=now() WHERE id=%s",
                    (status, schedule_id),
                ).rowcount
                > 0
            )
