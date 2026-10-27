"""POSIX process supervisor — the native host for detached agent processes.

An agent process is non-interactive: it talks to the world over DB + Redis and
logs to a file, so a host session for it no longer costs a PTY (macOS
caps `kern.tty.ptmx_max`, a whole-box wall the fleet hit long before any real
resource limit). This module hosts agents as named, detached processes tracked
by small on-disk records. Daemons run here too (services
need no PTY, and the per-box PTY ceiling then stops bounding service count);
only the agents' own persistent shells live elsewhere, in the machine's
pty-sessions service (`services/agent_runner/pty_sessions`, reached through
`base/sessions/pty`), which keeps the long-lived interactive pane.

The agent launch and the reap / status consumers use this surface:

- a "session" is a named process launched **double-forked** (via
  `base.native_process.reparent`) so it reparents to init immediately — a long-lived spawner
  (gateway / ops daemon) never accumulates it as a zombie, and no PTY is
  allocated;
- its identity (pid + start-time, to defeat pid recycling) is recorded as JSON
  under `$MEIVA_HOME/run/sessions/<name>.json`;
- stdout/stderr are redirected to `$MEIVA_HOME/logs/<name>.out.log` (+ an optional
  split stderr file) so a crash's last words survive the process ending;
- liveness = the record's pid is alive *and* its start-time matches the record;
- kill signals the process's whole group (graceful SIGTERM → the agent's finally
  runs; force SIGKILL) — a group signal also reaches children spawned mid-kill.

Agent liveness of record is `agents_meta.pid` in the DB — the respawn controller
reaps by that pid, and force-terminate kills by it. This record layer is the
*no-DB* fallback: `meiva stop` tears the gateway + DB down, then reaps the agents
it left running by enumerating these records.
"""

from __future__ import annotations

import contextlib
import signal
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

import psutil

from base.clock import Timers
from base.log import logger
from base.native_process.kernel_identity import pid_starttime_ticks
from base.native_process.ownership import stable_create_time
from base.native_process.process_control import SYSTEM_PROCESS_CONTROL, ProcessControl
from base.paths import logs_path, run_dir
from base.sessions.record import SessionRecord

__all__ = [
    "build_session_record",
    "graceful_signal",
    "has_session",
    "kill_session",
    "list_sessions",
    "new_session",
    "process_group_has_live_members",
    "session_log_path",
    "session_started_at",
]

# psutil exceptions that mean "the process is already gone / not ours to touch" —
# benign during a teardown race.
_GONE = (psutil.NoSuchProcess, psutil.AccessDenied, OSError)

# A pid is "the same process we launched" only if its start-time matches to
# within this tolerance — guards against the OS recycling the pid onto an
# unrelated process after ours exits.
_CREATE_TIME_TOLERANCE_S = 2.0

# create_time stamped on a session whose child died before the record was
# written. No real process start time is negative, so `process_for_record`'s
# start-time check can never match it — the record reads as dead from birth
# instead of claiming the pid's next occupant (see new_session).
DEAD_CHILD_SENTINEL = -1.0

# The reparent helper double-forks + execs in microseconds; it must never hang
# the spawner. A generous ceiling that only trips on a genuinely wedged fork.
_SPAWN_HELPER_TIMEOUT_S = 30.0

# Poll interval for the graceful group-empty wait in terminate_tree.
# (Name avoids the clock-lattice vocabulary; this is a polling cadence, not a clock.)
_KILL_POLL_S = 0.05


def _sessions_dir(home: Path) -> Path:
    d = run_dir(home) / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def record_path(name: str, *, home: Path) -> Path:
    return _sessions_dir(home) / f"{name}.json"


# meiva:note
# The combined stdout+stderr log of a session: `<home>/logs/<name>.out.log`. The path only; the launch creates the directory.
def session_log_path(name: str, *, home: Path) -> Path:
    return logs_path(home) / f"{name}.out.log"


def read_record(name: str, *, home: Path) -> SessionRecord | None:
    return SessionRecord.read(record_path(name, home=home))


def process_is_live(proc: psutil.Process) -> bool:
    """True when `proc` is genuinely running — a zombie counts as dead.

    `is_running()` alone is zombie-blind: a zombie's status is not DEAD, so a
    kill whose victim init has not reaped yet looks like a survivor. Same
    semantics as the orphan-reaper fix (#1272): a corpse awaiting its reaper
    cannot run again, so it is not a live session.
    """
    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def process_for_record(
    rec: SessionRecord,
    *,
    observed_starttime: int | None = None,
    has_observed_starttime: bool = False,
) -> psutil.Process | None:
    """Return the live psutil.Process named by `rec`, or None if it is gone or a
    different (recycled) process now holds the pid."""
    try:
        proc = psutil.Process(rec.pid)
        if not process_is_live(proc):
            return None
        if rec.starttime is not None:
            return (
                proc
                if rec.identifies(
                    rec.pid,
                    observed_starttime=observed_starttime,
                    has_observed_starttime=has_observed_starttime,
                )
                is True
                else None
            )
        # Platforms without Linux's clock-stable `/proc/<pid>/stat` field 22
        # identify the process by its creation time within a tolerance.
        if abs(stable_create_time(proc) - rec.create_time) > _CREATE_TIME_TOLERANCE_S:
            return None
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    return proc


def has_session(name: str, *, home: Path) -> bool:
    """True if the named session's process is still alive."""
    rec = read_record(name, home=home)
    if rec is None:
        return False
    return process_for_record(rec) is not None


def build_session_record(
    child_pid: int,
    *,
    cmd: str | list[str],
    cwd: Path,
    started_at: float | None = None,
    process_control: ProcessControl = SYSTEM_PROCESS_CONTROL,
) -> SessionRecord:
    try:
        proc = psutil.Process(child_pid)
        create_time = stable_create_time(proc)
        observed_started_at = proc.create_time()
    except psutil.NoSuchProcess:
        # meiva:note
        # A child that already exited frees its pid at its most reusable moment; stamping DEAD_CHILD_SENTINEL instead of time.time() prevents process_for_record from accepting the pid's next occupant.
        create_time = DEAD_CHILD_SENTINEL
        observed_started_at = 0.0
        starttime = None
    else:
        starttime = pid_starttime_ticks(child_pid)
    try:
        pgid = process_control.getpgid(child_pid)
    except (ProcessLookupError, OSError):
        pgid = None
    return SessionRecord(
        pid=child_pid,
        create_time=create_time,
        cmd=cmd if isinstance(cmd, str) else " ".join(cmd),
        cwd=str(cwd),
        started_at=observed_started_at if started_at is None else started_at,
        starttime=starttime,
        pgid=pgid,
    )


def new_session(
    name: str,
    cmd: str | list[str],
    cwd: Path,
    *,
    env: dict[str, str],
    home: Path,
    stderr_append: Path | None = None,
) -> bool:
    """Launch `cmd` as a detached, named background session.

    `cmd` is either a shell command line (`str`) run through `sh -c` or an
    already-split argv (`list[str]`) execed directly with no shell — the agent
    launcher passes argv so nothing it composes (an interpreter path with a
    space, the agent id) is ever re-parsed by a shell.
    `env` is the full child environment (the caller builds it). stdout+stderr go
    to the session log; `stderr_append`, when given, splits stderr to that file
    (the agent stderr log).

    The child is double-forked via `base.native_process.reparent` so it reparents to init and
    the spawner accretes no zombie. An existing live session of the same name is
    left untouched (idempotent), matching the has-session guard at the call site.

    Returns True on a successful spawn.

    Raises:
        RuntimeError: the reparent helper failed to launch / report a pid.
    """
    if has_session(name, home=home):
        return True
    argv = ["/bin/sh", "-c", cmd] if isinstance(cmd, str) else list(cmd)
    stdout_path = session_log_path(name, home=home)
    stderr_path = stderr_append if stderr_append is not None else stdout_path
    # `2>>{path}` / the helper's os.open do not mkdir; guarantee the dir on a
    # fresh machine so the launch cannot fail on a missing $MEIVA_HOME/logs.
    logs_path(home).mkdir(parents=True, exist_ok=True)

    # Run the reparent helper and wait for it (it exits in microseconds after
    # forking the real child). Waiting reaps the helper — the spawner's ONLY
    # direct child — so no zombie is left; the real child is already reparented
    # to init. The helper prints the child's pid to stdout.
    helper = [
        sys.executable,
        "-m",
        "base.native_process.reparent",
        str(stdout_path),
        str(stderr_path),
        *argv,
    ]
    result = subprocess.run(  # noqa: S603 — argv composed from repo-internal literals + int agent_id
        helper,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=_SPAWN_HELPER_TIMEOUT_S,
        check=False,
    )
    child_pid_str = result.stdout.strip()
    if result.returncode != 0 or not child_pid_str.isdigit():
        raise RuntimeError(
            f"reparent helper failed to launch session {name!r} "
            f"(exit {result.returncode}): stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    child_pid = int(child_pid_str)
    build_session_record(child_pid, cmd=cmd, cwd=cwd).write(record_path(name, home=home))
    return True


def admit_expected_signal(
    proc: psutil.Process,
    expected: SessionRecord,
    *,
    latest: SessionRecord | None,
    observed_ticks: Sequence[int | None] | None,
    first_tick: int | None,
    has_first_tick: bool,
) -> bool:
    if latest != expected:
        return False
    if expected.starttime is not None:
        has_second_tick = observed_ticks is not None and len(observed_ticks) >= 2
        second_tick = (
            observed_ticks[1] if has_second_tick and observed_ticks is not None else first_tick
        )
        return (
            expected.identifies(
                proc.pid,
                observed_starttime=second_tick,
                has_observed_starttime=has_first_tick or has_second_tick,
            )
            is True
        )
    return abs(stable_create_time(proc) - expected.create_time) <= _CREATE_TIME_TOLERANCE_S


def graceful_signal(
    name: str,
    *,
    expected: SessionRecord | None = None,
    home: Path,
    observed_ticks: Sequence[int | None] | None = None,
) -> bool:
    """Send the graceful-stop signal (SIGTERM) to the session's process without
    waiting or force-killing.

    The agent converts SIGTERM to SystemExit and runs its finally (closing the
    MCP daemon + Claude Code subprocess it tracks). Returns True if a live
    process was signaled. `meiva stop`'s reap uses this to SIGTERM every agent, then
    wait on all of them under one shared deadline before force-killing stragglers
    — so the teardown is O(slowest agent), not O(sum of agents).
    """
    rec = read_record(name, home=home)
    if rec is None or (expected is not None and rec != expected):
        return False
    has_first_tick = observed_ticks is not None and len(observed_ticks) >= 1
    first_tick = observed_ticks[0] if has_first_tick and observed_ticks is not None else None
    proc = process_for_record(
        rec,
        observed_starttime=first_tick,
        has_observed_starttime=has_first_tick,
    )
    if proc is None:
        return False
    with contextlib.suppress(*_GONE):
        if expected is not None and not admit_expected_signal(
            proc,
            expected,
            latest=read_record(name, home=home),
            observed_ticks=observed_ticks,
            first_tick=first_tick,
            has_first_tick=has_first_tick,
        ):
            return False
        # Signal only this captured process object, never resolve the name a
        # second time into a replacement target. psutil also guards PID reuse.
        proc.terminate()  # SIGTERM to the agent only; its finally closes its children
        return True
    return False


def _pgid_of(
    proc: psutil.Process,
    *,
    process_control: ProcessControl = SYSTEM_PROCESS_CONTROL,
) -> int | None:
    """The process group the session process lives in, or None when it is gone.

    ``base.native_process.reparent`` setsid()s the helper before forking, so the launched
    process — and every descendant that does not deliberately leave the group —
    shares one pgid (the helper's pid; the helper itself exits at once).
    psutil has no pgid accessor, so this reads it via ``os.getpgid``."""
    with contextlib.suppress(OSError):
        return process_control.getpgid(proc.pid)
    return None


def _signal_group(
    pgid: int | None,
    sig: int,
    *,
    process_control: ProcessControl = SYSTEM_PROCESS_CONTROL,
) -> None:
    """Send `sig` to every process in the group, or nothing when it is unknown.

    A group signal reaches children spawned after any psutil snapshot — the
    hard-kill window the old children-list walk missed (task #2249). A dead
    leader frees its pid for reuse; a reused pid that setsid()s into a new
    group could then be hit here (µs window, same shape as pty_sessions
    group kills — accepted)."""
    if pgid is None or pgid <= 0:
        return
    with contextlib.suppress(OSError):
        process_control.killpg(pgid, sig)


def process_group_has_live_members(
    pgid: int | None,
    *,
    process_control: ProcessControl = SYSTEM_PROCESS_CONTROL,
    processes: Iterable[psutil.Process] | None = None,
) -> bool:
    """Whether an observed non-zombie process belongs to numeric group `pgid`.

    None, a nonpositive group ID, or a gone/zombie-only group returns False.
    A surviving group is scanned after the zero-signal probe: macOS can reject
    that probe for a zombie-only group, and zombies await their reaper without
    keeping the session alive. Members that disappear or cannot be inspected
    are skipped, as in the session supervisor's best-effort liveness check.

    This is a momentary observation, not process identity, signal authority or
    closure proof. Group IDs can be reused and members can change during the
    scan; False does not certify absence when visibility is incomplete.
    """
    if pgid is None or pgid <= 0:
        return False
    try:
        process_control.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # noqa: S110 - macOS EPERM for a zombie-only group falls through to the member scan.
        pass
    candidates = psutil.process_iter() if processes is None else processes
    for process in candidates:
        try:
            if (
                process_control.getpgid(process.pid) == pgid
                and process.status() != psutil.STATUS_ZOMBIE
            ):
                return True
        except (psutil.Error, ProcessLookupError):
            continue
    return False


def terminate_tree(
    proc: psutil.Process,
    *,
    graceful: bool,
    timeout: float,
    timers: Timers,
    process_control: ProcessControl = SYSTEM_PROCESS_CONTROL,
    wait_timeout: float = 5.0,
) -> bool:
    """Terminate `proc` and its whole process group.

    The session process runs in its own process group (``base.native_process.reparent``
    setsid()s the helper; the launched process and every descendant that does
    not deliberately leave the group share that pgid). Signaling the GROUP —
    SIGTERM to all members, wait, SIGKILL to all members — closes the window
    the old psutil-only walk left open: a child spawned DURING the graceful
    wait (a bash wrapper's foreground command, a process an unwound finally
    spawns) was not in the pre-captured children list, so the hard-kill walk
    missed it and it survived as an orphan (task #2249).

    graceful: first SIGTERM **the whole group** and wait up to `timeout` for
    it to empty — the process converts SIGTERM to a
    KeyboardInterrupt/SystemExit unwind and runs its finally (an agent closes
    the MCP daemon + Claude Code subprocess it tracks; a service daemon closes
    its pidfile and pools), and its children die with it. Whatever is still
    alive after the wait is hard-killed.
    force: SIGKILL the whole group immediately.

    Returns True when the graceful signal alone emptied the group AND every
    pre-existing descendant — the caller reports that as the `mode`, so an
    escalation to SIGKILL cannot be mislabelled a clean stop.
    """
    # Snapshot BEFORE the graceful signal: a descendant that deliberately left
    # the group (a setsid'd worker) is unreachable by group signal, and once
    # the leader dies it reparents to init — this walk is the only handle on
    # it, so the graceful verdict checks it and the hard kill reaps it. An
    # already-gone leader yields an empty list.
    children: list[psutil.Process] = []
    with contextlib.suppress(*_GONE):
        children = proc.children(recursive=True)
    pgid = _pgid_of(proc, process_control=process_control)
    if graceful:
        # SIGTERM the whole group, not just the top process: a bash wrapper
        # delays its own SIGTERM until its foreground command ends, and a
        # command an unwound finally spawns mid-wait is not in any children
        # snapshot — only a group signal reaches both.
        _signal_group(pgid, signal.SIGTERM, process_control=process_control)
        deadline = timers.monotonic() + timeout
        while timers.monotonic() < deadline:
            if (
                not process_is_live(proc)
                and not process_group_has_live_members(pgid, process_control=process_control)
                and all(not process_is_live(c) for c in children)
            ):
                return True
            timers.sleep(_KILL_POLL_S)
    # Hard kill: the group first (a SIGTERM-ignoring straggler or a child
    # spawned during the graceful wait dies here), then the pre-captured walk
    # for a descendant that left the group (a setsid'd worker) — the group
    # signal cannot reach it, and once the leader dies it reparents to init.
    _signal_group(pgid, signal.SIGKILL, process_control=process_control)
    for child in [*children, proc]:
        with contextlib.suppress(*_GONE):
            process_control.kill(child.pid, signal.SIGKILL)
    if wait_timeout > 0:
        with contextlib.suppress(*_GONE):
            psutil.wait_procs([proc, *children], timeout=wait_timeout)
    return False


def kill_session(
    name: str,
    *,
    graceful: bool = False,
    timeout: float = 15.0,
    timers: Timers,
    home: Path,
    process_control: ProcessControl = SYSTEM_PROCESS_CONTROL,
    wait_timeout: float = 5.0,
) -> tuple[bool, str]:
    """Stop the named session and forget it (force / graceful).

    Returns (ok, mode) with mode in {graceful, forced, noop}. `mode` reports what
    actually happened, not what was asked for: a graceful stop the process
    ignored until the SIGKILL fallback comes back `forced`, so the caller's
    escalation marker fires instead of the clean-stop one hiding a hard kill.

    Idempotent: an absent/dead session is a noop.
    """
    rec = read_record(name, home=home)
    if rec is None:
        return True, "noop"
    proc = process_for_record(rec)
    if proc is None:
        record_path(name, home=home).unlink(missing_ok=True)
        return True, "noop"
    ended_on_signal = False
    try:
        ended_on_signal = terminate_tree(
            proc,
            graceful=graceful,
            timeout=timeout,
            timers=timers,
            process_control=process_control,
            wait_timeout=wait_timeout,
        )
    except Exception as exc:
        logger.warning("posixproc kill {name} hit {exc}", name=name, exc=exc)
    mode = "graceful" if ended_on_signal else "forced"
    # Kill result re-check (#1015): a kill that reports
    # success it did not achieve turns a live-but-unbacked session into a
    # service nothing starts. The contract (session_backend.py) says `ok`
    # means the session is confirmed gone; on a survivor we keep the record —
    # the no-DB reap's only view of the process — and say so. A zombie is not
    # a survivor: it is dead, just awaiting its parent's reap.
    if process_is_live(proc):
        logger.error(
            "posixproc kill {name}: pid {pid} is still running after the kill — leaving "
            "its session record in place so the survivor stays visible",
            name=name,
            pid=proc.pid,
        )
        return False, mode
    record_path(name, home=home).unlink(missing_ok=True)
    return True, mode


def session_started_at(name: str, *, home: Path) -> float | None:
    """Epoch seconds when the named session was launched, or None when it is
    not alive (no record, or the record's process is gone).

    Read off the on-disk record — the same source has_session / list_sessions
    judge — so a liveness consumer can render an uptime without a second probe.
    """
    rec = read_record(name, home=home)
    if rec is None:
        return None
    if process_for_record(rec) is None:
        return None
    return rec.started_at


def record_reapable(rec: SessionRecord) -> tuple[bool, str, int | None]:
    """Whether a failed liveness check proves a record can be discarded.

    A dead leader whose recorded process group still has live members keeps
    its record: the group occupancy is the only durable proof that the
    surviving descendants belong to this session (issue #2123). Deleting the
    record here would let a later stop silently certify a unit whose port a
    surviving orphan still answers on.

    Ordering boundary (task #2898): the group gate runs BEFORE the reuse
    criterion, so a record whose pid was recycled by an unrelated process
    while its recorded group is still occupied is retained — and later
    listed, warned about on every listing — on the group's strength alone;
    the reuse criterion never runs for it. The order is deliberate: a
    recycled pid proves the LEADER is gone, not that its group is empty, and
    checking reuse first would reap exactly the records whose surviving
    descendants are the orphans #2123 preserves. A recycled pid whose
    recorded group is empty (or absent) is still reaped by
    the reuse criterion below.

    Returns:
        (reapable, why, occupied_group): `occupied_group` carries the
        recorded pgid whose live members are keeping the record — the one
        retention case `list_sessions` lists — so the caller does not have to
        scan the group a second time; None in every other outcome.
    """
    if process_for_record(rec) is not None:
        return False, "process is still live", None
    if rec.pgid is not None and process_group_has_live_members(rec.pgid):
        return (
            False,
            f"leader is gone but process group {rec.pgid} still has live members "
            "(surviving descendants keep the ownership record)",
            rec.pgid,
        )
    try:
        proc = psutil.Process(rec.pid)
        if not proc.is_running() or not process_is_live(proc):
            return True, "pid is no longer running", None
    except psutil.NoSuchProcess:
        return True, "pid is gone", None
    except (psutil.AccessDenied, OSError):
        return False, "pid could not be inspected", None
    if rec.identifies(rec.pid) is False:
        return True, "pid was reused by another process", None
    return False, "live pid did not satisfy the identity check", None


def list_sessions(prefix: str = "", *, home: Path) -> list[str]:
    """Names of all live sessions, optionally filtered by `prefix`.

    Reaps records whose process is gone so the listing reflects reality —
    except a record whose recorded process group still has live members: that
    name stays listed because its surviving descendants are still owned and a
    stop must converge them, not lose them (issue #2123). A retained record
    warns on every listing — the incident state stays loud until a stop
    converges the group; `record_reapable` documents the pid-reuse ordering
    boundary that warning can surface.
    """
    out: list[str] = []
    for rec_file in _sessions_dir(home).glob("*.json"):
        name = rec_file.stem
        if prefix and not name.startswith(prefix):
            continue
        rec = SessionRecord.read(rec_file)
        if rec is None:
            rec_file.unlink(missing_ok=True)
        elif process_for_record(rec) is not None:
            out.append(name)
        else:
            reapable, why, occupied_group = record_reapable(rec)
            if not reapable:
                logger.warning(
                    "posixproc retaining live session record {name}: {why}",
                    name=name,
                    why=why,
                )
                # Only the occupied-group case is listed: it carries the
                # durable ownership proof a stop must converge. A live pid
                # with an uncertain identity stays invisible — nothing
                # provable may be claimed from it. The group occupancy was
                # already read inside record_reapable; never scan it again
                # per listing (task #2898).
                if occupied_group is not None:
                    out.append(name)
                continue
            rec_file.unlink(missing_ok=True)
    return out
