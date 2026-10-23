# meiva:note
# Schedule execution engine, composed by ``services.wake.schedule_manager.runner``.
#
# Loads schedule ``<id>`` from the DB, materializes its script under
# ``$MEIVA_HOME/schedules/<id>/``, binds a ``schedule:<id>`` actor identity (so the
# script's ``meiva.agents.*`` calls attribute to the schedule instead of failing for
# lack of an agent id), then runs it. A ``.py`` script runs in-process via runpy so
# it shares the bound actor; any other command runs as a subprocess. An uncaught
# crash's traceback is written to ``schedules.last_error`` (the latest crash) and, truncated
# to its last 3000 characters, to the run's ``schedule_runs.note`` (one
# per crash).
#
# Version-controlled schedule templates (manifest + scripts) live in
# ``schedules/`` — provisioned via ``base/daemon/schedules/builtin_schedules.py``.
#
# Every process execution appends one row to ``schedule_runs`` (the run-history
# drawer's data source): opened with ``ok = NULL`` (in-progress) when the runner
# starts, closed with the outcome when it exits. This is process-level history —
# one row per process lifetime, not per fire — and it is severable observability:
# a run-record write failure never affects the schedule itself. A run the runner
# cannot close (a SIGTERM/SIGHUP kill, a manager SIGKILL on stop/restart/
# edited-script save) stays ``ok = NULL`` until the ScheduleManager next
# launches or reaps that schedule's session and closes it as ``interrupted``; no
# periodic sweep closes it. A stall-guard hard exit reaps owned descendants,
# then attempts to close it ``ok = false`` within a bounded record deadline.
#
# The ScheduleManager launches this inside a session named
# ``meiva-schedule-<id>`` with one admitted launch per desired revision. A schedule is a supervised resident process, so an exit-0 is treated as
# a deliberate finish: the runner records ``status='completed'`` and the manager
# leaves it alone. A nonzero exit / uncaught exception records its traceback in
# ``schedules.last_error``. Process loss becomes terminal error; only an explicit
# new revision authorizes another launch. A deliberate kill (SIGTERM/SIGHUP) writes nothing and is not counted a crash.

from __future__ import annotations

import faulthandler
import os
import runpy
import selectors
import shlex
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any

from loguru import logger

import base.host.proc
import meiva
from base.clock import Clock
from base.config import ConfigBoot
from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database
from services.wake.schedule_manager.guard_admission import (
    Admission,
    Frame,
    ParkFiles,
    admit_stall,
    close_failure_is_fatal,
    is_parked,
    stop_admission,
)
from services.wake.schedule_manager.script_outcome import (
    command_failure_message,
    command_timeout_message,
    crash_note,
    exit_code_of,
    exit_detail,
)
from services.wake.schedule_manager.stall_policy import (
    Signature,
    StallPolicy,
    Watch,
    check_interval_s,
    close_wait_s,
    stall_message,
    stall_note,
    watch_step,
)


# meiva:note
# What ``meiva.ensure_plugins_loaded`` is given to install the plugin namespaces.
@dataclass(frozen=True)
class PluginInputs:
    config: ConfigBoot
    clock_factory: Callable[[], Clock]
    producer: Callable[[], Any]


# meiva:note
# What the runner's composition root hands the engine for one process execution.
@dataclass(frozen=True)
class RunnerRoot:
    database: Database
    inputs: ScheduleInputs
    plugins: PluginInputs
    policy: StallPolicy
    home: Path


# A .py schedule script is run in-process, so a single call that hangs (a
# wedged gateway, a black-holed DB connection, a stuck import) parks the whole
# runner with no crash and no last_error — 2026-08-03: the self-evolution
# weekly schedule sat "alive" through the gateway's 08:28-09:23 freeze window
# and silently missed its Monday 9am fire. The stall guard watches the main
# meiva:note
# thread's stack and hard-exits after a frame has not advanced for
# `StallPolicy.timeout_s`, so the schedule is recorded as failed (last_error)
# instead of a zombie. The verdicts are the pure functions of `stall_policy` and
# `guard_admission`; this module samples, locks and acts.

# Cache the code's filenames, the same identities the sampled frames carry.
# Unlike module __file__, these also work with loaders that omit __file__;
# a missing attribute must not turn every guard tick into a skipped check.
_PARK_FILES = ParkFiles(
    subprocess=subprocess.Popen[bytes].wait.__code__.co_filename,
    selectors=selectors.SelectSelector.select.__code__.co_filename,
)


def schedule_dir(home: Path, schedule_id: int) -> Path:
    return home / "schedules" / str(schedule_id)


# Filename suffixes that mean "this token IS the script" — a bare token with
# any other dotted name (a versioned interpreter like ``python3.11``, an
# extensionless binary path, a dotted flag value) must not be mistaken for
# the script file the runner materializes (audit gateway.md P2-8).
_SCRIPT_SUFFIXES = frozenset({".py", ".sh", ".js", ".bash", ".zsh"})


def script_filename(command: str) -> str:
    """The file the script is written to — the first token in ``command``
    whose name ends in a known script extension, else ``schedule.py``. So
    ``python schedule.py`` -> ``schedule.py``; ``bash run.sh`` -> ``run.sh``;
    ``python3.11 main.py`` -> ``main.py`` (not ``python3.11``)."""
    for token in shlex.split(command):
        name = Path(token).name
        if name.endswith(tuple(_SCRIPT_SUFFIXES)) and not token.startswith("-"):
            return name
    return "schedule.py"


def load(
    database: Database, schedule_id: int, revision: int | None = None
) -> tuple[str, str] | None:
    """Return (script, command) for an enabled schedule, or None if it is gone /
    disabled (a benign race: the manager launched it, then it was deleted)."""
    if revision is None:
        with database.connect(autocommit=True) as conn:
            row = conn.execute(
                "SELECT script, command FROM schedules WHERE id = %s AND enabled = true",
                (schedule_id,),
            ).fetchone()
    else:
        # Admission fences a delayed runner from executing a newer script under
        # an old command identity. Mark applied before any user code so a quick
        # completion survives the manager's post-launch acknowledgement crash.
        with database.write_transaction() as conn:
            row = conn.execute(
                "UPDATE schedules SET applied_revision = %s, status = 'running', last_error = NULL "
                "WHERE id = %s AND enabled AND desired_revision = %s RETURNING script, command",
                (revision, schedule_id, revision),
            ).fetchone()
    return (row[0], row[1]) if row is not None else None


def record_error(database: Database, schedule_id: int, message: str) -> None:
    with database.write_transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE schedules SET last_error = %s, updated_at = now() WHERE id = %s",
            (message, schedule_id),
        )


def _mark_completed(database: Database, schedule_id: int) -> None:
    """Record a clean exit (rc=0) as the terminal `completed` status. A schedule
    is a supervised resident process, so an exit-0 is a deliberate finish, not a
    crash — this is the durable signal the ScheduleManager reads to leave the
    schedule completed rather than recording process loss as an error.
    The manager reads liveness before status, so a session that is gone is
    guaranteed to have this write already committed (see services/wake/schedule_manager/manager.py)."""
    with database.write_transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE schedules SET status = 'completed', updated_at = now() WHERE id = %s",
            (schedule_id,),
        )


def finish_completed(database: Database, schedule_id: int, run_id: int | None) -> None:
    """Record a clean finish: mark the schedule completed and close the run
    row ok=true. The completed-marker write is best-effort — if it fails, the
    manager may record process loss as an error, but does not relaunch the
    schedule. The run row must still read as a success: the run itself
    finished, only the bookkeeping lost a write (QA P3-5 — it must not be
    recorded as 'crashed: OperationalError')."""
    try:
        _mark_completed(database, schedule_id)
    except Exception:
        logger.exception("schedule {} completed-marker write failed", schedule_id)
        record_run_end(database, run_id, ok=True, note="completed-marker write failed")
    else:
        record_run_end(database, run_id, ok=True, note=None)


def record_run_start(database: Database, schedule_id: int) -> int | None:
    """Open a run-history row for this process execution (ok = NULL, in-progress).

    Returns the run id, or None when the write fails — run history is severable
    observability, so a DB hiccup must never break the schedule itself (the
    caller then skips the closing write)."""
    try:
        with database.write_transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schedule_runs (schedule_id) VALUES (%s) RETURNING id",
                (schedule_id,),
            )
            row = cur.fetchone()
            return row[0] if row is not None else None
    except Exception:
        logger.exception("schedule {} run-record start failed", schedule_id)
        return None


def record_run_end(database: Database, run_id: int | None, *, ok: bool, note: str | None) -> None:
    """Close a run-history row with its outcome. No-op when the start write
    failed (run_id is None); a failure here is likewise never fatal."""
    if run_id is None:
        return
    try:
        with database.write_transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE schedule_runs SET ok = %s, note = %s WHERE id = %s",
                (ok, note, run_id),
            )
    except Exception:
        logger.exception("schedule run-record end failed (run {})", run_id)


_ORIGINAL_SLEEP = time.sleep


def patch_park_detection() -> None:
    """Wrap ``time.sleep`` so the stall guard can see a deliberate park.

    ``time.sleep`` is a C builtin: the deepest *Python* frame of a main thread
    blocked inside it is the caller of sleep — a stable frame, exactly like a
    stall. The wrapper is named ``sleep`` (a ``_PARK_FRAME_NAMES`` member), so
    a main thread parked in it reads as parked, while a thread stuck in any
    other call still reads as stalled. Semantics are unchanged — the wrapper
    just forwards to the original."""

    def sleep(delay: float) -> None:
        _ORIGINAL_SLEEP(delay)

    time.sleep = sleep


def restore_park_detection() -> None:
    """Undo ``patch_park_detection``: put the stdlib ``time.sleep`` back.

    The wrapper exists only for the stall guard's judgment window; leaving it
    installed past the guard's stop swaps `time.sleep` for a Python function
    process-wide in this runner (the runner is its own process — one schedule
    per `python -m services.wake.schedule_manager.runner <id>` session — but a
    ``.py`` schedule script runs in-process here, so the swap would leak into
    the rest of its run), which a later ``assert time.sleep is _REAL_SLEEP``
    guard in the test suite trips on. Idempotent and safe to call without a
    patch."""
    time.sleep = _ORIGINAL_SLEEP


def stall_action(
    database: Database, schedule_id: int, message: str, run_id: int | None, *, policy: StallPolicy
) -> None:
    """Reap descendants, record the failure within a fixed deadline, hard-exit 1."""
    try:
        try:
            # Snapshot descendants while ancestry still proves ownership. Retain
            # identities through TERM/KILL; never signal the shared PTY group.
            # setsid stays covered; already-reparented daemons are exempt.
            base.host.proc.kill_process_tree(os.getpid(), include_root=False)
        except Exception:
            logger.exception("Schedule {} child cleanup failed", schedule_id)
        logger.error("Schedule {} {}", schedule_id, message)
        # One interpreter-level deadline covers both writes: if the DB never
        # answers, faulthandler dumps every thread's stack (the hang's location)
        # and exits 1.
        faulthandler.dump_traceback_later(policy.record_deadline_s, exit=True)
        try:
            record_error(database, schedule_id, message)
        except Exception:
            logger.opt(exception=True).warning(
                "Schedule {} stall message could not be recorded in last_error; exiting without it",
                schedule_id,
            )
        record_run_end(database, run_id, ok=False, note=stall_note(policy))
    finally:
        os._exit(1)  # hard exit; the manager records the process loss


def _frame_facts(frame: FrameType) -> Frame:
    parent = frame.f_back
    return Frame(
        filename=frame.f_code.co_filename,
        function=frame.f_code.co_name,
        parent_filename=None if parent is None else parent.f_code.co_filename,
        parent_function=None if parent is None else parent.f_code.co_name,
    )


class StallGuard:
    """Own frame observation and serialize script completion with stall admission.

    Sleep-family parks and actual subprocess waits retain their unlimited park
    contract. A child wait without timeout remains an accepted boundary. Spawn,
    argument conversion, stdin flush and selectors outside subprocess stay
    guarded. An unexpected guard failure is retained and raised by ``close``.
    """

    def __init__(
        self, database: Database, schedule_id: int, run_id: int | None, *, policy: StallPolicy
    ) -> None:
        self.database = database
        self.schedule_id = schedule_id
        self.run_id = run_id
        self.policy = policy
        self.main_thread_id = threading.get_ident()
        self.stop = threading.Event()
        self.admission = threading.Lock()
        self._admitted = Admission()
        self.error: BaseException | None = None
        self.thread = threading.Thread(
            target=self._guard, name=f"schedule-{schedule_id}-stall-guard", daemon=True
        )
        self.thread.start()

    @property
    def action_started(self) -> bool:
        with self.admission:
            return self._admitted.stalled

    def _guard(self) -> None:
        try:
            self._observe()
        except BaseException as exc:
            self.error = exc
            logger.opt(exception=exc).error("Schedule {} stall guard failed", self.schedule_id)

    def sample(self) -> Signature | None:
        frame = sys._current_frames().get(self.main_thread_id)
        if frame is None or is_parked(_frame_facts(frame), _PARK_FILES):
            return None
        return frame.f_code.co_filename, frame.f_lineno, frame.f_code.co_name

    def _claim_stall(self) -> bool:
        with self.admission:
            self._admitted, admitted = admit_stall(self._admitted)
            return admitted

    def _observe(self) -> None:
        watch: Watch | None = None
        while not self.stop.wait(check_interval_s(self.policy)):
            signature = self.sample()
            watch, stood_s = watch_step(watch, signature, time.monotonic(), self.policy)
            if stood_s is not None and signature is not None:
                if self._claim_stall():
                    stall_action(
                        self.database,
                        self.schedule_id,
                        stall_message(stood_s, signature),
                        self.run_id,
                        policy=self.policy,
                    )
                return

    def close(self, timeout: float = 1.0) -> None:
        # Closing admission and claiming a real stall share one decision point.
        # Never hold this lock through cleanup or a synchronous database write.
        with self.admission:
            self._admitted, action_started = stop_admission(self._admitted)
            self.stop.set()
        # Ordinary stop wakes immediately. An admitted action keeps the 3s TERM
        # + 5s reap and the record deadline before its hard exit.
        self.thread.join(timeout=close_wait_s(self.policy) if action_started else timeout)
        alive = self.thread.is_alive()
        if self.error is not None:
            raise self.error
        if alive:
            raise RuntimeError(f"Schedule {self.schedule_id} stall guard did not stop")


def run_python_script(
    schedule_id: int, run_id: int | None, script_path: Path, root: RunnerRoot
) -> None:
    """Run with isolated argv and collect the watchdog before any terminal write."""
    # Load plugin namespaces (meiva.tasks etc.) into this process before the
    # in-process script runs. This runner never builds the agent graph, so
    # nothing else loads plugins here; without this, a schedule script that
    # touches meiva.tasks would hit the factory `import meiva` and AttributeError.
    # Only the .py-in-process branch needs it — the other branch runs a
    # non-.py shell command (bash etc.) that does not import meiva.
    # Stall guard: a hung call inside the script (or in plugin
    # loading below) must not leave the runner alive-but-silent
    # (2026-08-03 self-evolution miss). Started before plugin loading
    # so an import hang is covered too. Stopped before _mark_completed
    # so a clean return cannot be overtaken by a spurious kill.
    patch_park_detection()
    guard = StallGuard(root.database, schedule_id, run_id, policy=root.policy)
    # The gateway launches this runner as `python -m services.wake.schedule_manager.runner
    # <id>`, so sys.argv carries the schedule id. The script must not
    # inherit that runner-only argv: hand it the argv `python <script>`
    # would produce — just its own path — and restore the runner's argv
    # afterwards (2026-09-22: the daily debt sweep's argparse rejected the
    # leaked id and exited 2 on every launch, tripping the crash breaker
    # before its first fire).
    runner_argv = sys.argv
    sys.argv = [str(script_path)]
    try:
        meiva.ensure_plugins_loaded(
            config=root.plugins.config,
            clock_factory=root.plugins.clock_factory,
            producer=root.plugins.producer,
        )
        runpy.run_path(
            str(script_path),
            run_name="__main__",
            init_globals={"MEIVA_SCHEDULE_INPUTS": root.inputs},
        )
    finally:
        sys.argv = runner_argv
        primary = sys.exc_info()[1]
        try:
            guard.close()
        except BaseException:
            if close_failure_is_fatal(primary):
                raise
            # Successful SystemExit is completion, so cleanup failure wins.
            # Preserve a failed script's primary failure; the guard keeps
            # its original error and already reported it immediately.
            logger.opt(exception=True).error("Schedule {} guard close failed", schedule_id)
        finally:
            restore_park_detection()
            if guard.action_started:
                # A real stall won admission before script completion.
                # Never mark it completed, even if cleanup exhausted
                # meiva:note
                # the bounded join.
                os._exit(1)


def _record_script_exit(
    database: Database, schedule_id: int, run_id: int | None, exc: SystemExit
) -> int:
    """Record a .py script's deliberate sys.exit() like a command's exit code
    (QA P3-3 — it must not leave the run row in-progress forever). A None code
    is 0; a non-int code (a message string) is 1. int() normalizes bools (the
    interpreter treats them as exit codes: True -> 1, False -> 0); a non-int
    code is a message the interpreter would print to stderr, so it rides the
    note instead of being swallowed (QA N1)."""
    code = exit_code_of(exc.code)
    if code == 0:
        finish_completed(database, schedule_id, run_id)
    else:
        message = exit_detail(exc.code)
        record_error(database, schedule_id, f"script exited {code}{message}")
        record_run_end(database, run_id, ok=False, note=f"script exited {code}{message}")
    return code


def run(schedule_id: int, revision: int | None, root: RunnerRoot) -> int:
    """Materialize + run the schedule. Returns a process exit code."""
    database = root.database
    loaded = load(database, schedule_id, revision)
    if loaded is None:
        logger.warning("Schedule {} is gone or disabled; nothing to run", schedule_id)
        return 0
    script, command = loaded

    work_dir = schedule_dir(root.home, schedule_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    script_name = script_filename(command)
    script_path = work_dir / script_name
    script_path.write_text(script)

    # meiva:note
    # Run history: one row per process execution, opened in-progress (ok=NULL)
    # here and closed with the outcome on every exit path below — including the
    # stall guard, which receives run_id and closes the row ok=false within its
    # bounded record deadline before hard exit; a deadline that expires abandons
    # the write. Both an abandoned write and a kill that leaves no code path to
    # close (SIGTERM/SIGHUP/SIGKILL) leave the row in-progress; the manager closes
    # it as 'interrupted' when it next launches or reaps the schedule.
    run_id = record_run_start(database, schedule_id)

    try:
        if script_name.endswith(".py"):
            run_python_script(schedule_id, run_id, script_path, root)
            # meiva:note
            # clean return => finished, not crashed
            finish_completed(database, schedule_id, run_id)
            return 0
        # meiva:note
        # A non-.py command runs as a child process — the stall guard's main-
        # thread frame watch cannot see inside it, and the runner parked in
        # subprocess.run would read as a legitimate park anyway (a subprocess
        # frame marks a child wait). Bound it with the same stall timeout: a
        # command that has not finished within the budget is hung, not long-running —
        # without a bound, a never-exiting command would sit forever with no
        # last_error and no breaker fire, silently eating every future fire
        # window (2026-08-08 audit, P2-2 — the .py branch got its stall guard
        # after the 2026-08-03 self-evolution miss; the command branch was
        # still open). subprocess.run kills the child on expiry and raises
        # TimeoutExpired; the manager reports terminal process loss without replay.
        stall_timeout_s = root.policy.timeout_s
        try:
            # meiva:note
            # command is the operator-authored schedule command.
            result = subprocess.run(  # noqa: S603
                shlex.split(command),
                cwd=str(work_dir),
                check=False,
                timeout=stall_timeout_s,
            )
        except subprocess.TimeoutExpired:
            message = command_timeout_message(stall_timeout_s, command)
            record_error(database, schedule_id, message)
            logger.error("Schedule {} {}", schedule_id, message)
            record_run_end(
                database, run_id, ok=False, note=f"stall timeout ({stall_timeout_s:.0f}s)"
            )
            return 1
        if result.returncode != 0:
            record_error(database, schedule_id, command_failure_message(result.returncode, command))
            record_run_end(database, run_id, ok=False, note=f"command exited {result.returncode}")
        else:
            finish_completed(database, schedule_id, run_id)
        return result.returncode
    except SystemExit as exc:
        return _record_script_exit(database, schedule_id, run_id, exc)
    except Exception as exc:
        tb = traceback.format_exc()
        record_error(database, schedule_id, tb)
        logger.error("Schedule runner execution failed: {}", tb)
        record_run_end(database, run_id, ok=False, note=crash_note(type(exc).__name__, tb))
        return 1
