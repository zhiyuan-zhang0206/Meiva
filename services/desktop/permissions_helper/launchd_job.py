"""The launchd job surface of the macOS permissions helper.

One home for the per-cluster LaunchAgent identity — label, plist path, launchd
domain — and for bounded ``launchctl print`` inspection of the job. The
build/cert/repair steps stay in ``lifecycle``; that module delegates the job
identity here so the label formula exists once, and the helper healthcheck
(task #3393) reads and parses the job here so the ``launchctl print`` field
vocabulary exists once too. Exact-home retirement (``meiva stop``) lives here, and
so does ``require_retired_helper``, which admits replacing a stale installed
artifact only once that retirement is complete.

The parse reads the ``job state`` line deliberately: a stuck job's top-level
``state`` still reads ``spawn scheduled``/``xpcproxy`` — ``spawn failed`` lives
only in ``job state`` (F5 findings section 6; PR #2500 review). Reads are
bounded and total: a hung tool or an absent job folds into a sentinel result,
never an exception — the callers are healthchecks whose whole point is to
answer even when the system under them does not.
"""

from __future__ import annotations

import errno
import os
import plistlib
import re
import socket
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeGuard

import psutil

from base.clock import Timers
from base.host.permissions_helper.client import HelperAtSocket
from base.host.system.hostport import Host
from base.native_process.ownership import OwnedProcess, capture_tree, leader_owns_pids

HELPER_BUNDLE_ID = "com.meiva.permissions-helper"
"""Fixed across clusters so one TCC grant covers all (the grant keys on this)."""

_READ_TIMEOUT_S = 30.0
"""Bound for one launchctl query — local IPC with launchd."""

_TIMED_OUT_RC = 124
"""Stand-in exit status for a read the bound killed (`timeout(1)`'s convention;
the real launchd codes are far below this)."""


class BoundedRunner(Protocol):
    def __call__(
        self,
        cmd: Sequence[str],
        /,
        *,
        timeout: float,
        capture_output: bool = ...,
    ) -> subprocess.CompletedProcess[bytes]: ...


# meiva:note
# What the helper's launchd job identity reads of the world, read once by the entry that runs the
# command: the real user id (the launchd domain is `gui/<uid>`), the platform system (`"darwin"`
# or `"linux"`; only macOS has a helper) and the OS account home (the LaunchAgents directory lives
# under it). The command runner and the timers are separate arguments, not fields: they are
# ports, not facts. The retiring process's own id is not a fact of the job, so retirement takes
# it as `caller_pid`.
@dataclass(frozen=True, slots=True)
class LaunchdFacts:
    uid: int
    platform: str
    user_home: Path

    @classmethod
    def of(cls, host: Host, *, user_home: Path) -> LaunchdFacts:
        return cls(uid=host.uid, platform=host.platform.system, user_home=user_home)

    @property
    def domain(self) -> str:
        return helper_job_domain(self.uid)

    @property
    def agents_dir(self) -> Path:
        return helper_job_agents_dir(self.user_home)


def _is_str_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _remaining_sleep(deadline: float, timers: Timers) -> float:
    return min(0.05, max(0.0, deadline - timers.monotonic()))


def helper_job_label(home: Path) -> str:
    """This cluster's helper LaunchAgent label (``<bundle id>.<home slug>``).

    Per-cluster, keyed on the home-path slug (path-only identity); the bundle id
    (the TCC grant) stays shared across clusters.
    """
    from base.cluster import home_slug

    return f"{HELPER_BUNDLE_ID}.{home_slug(home)}"


# meiva:note
# The launchd domain this login session's jobs live in (`gui/<uid>`).
def helper_job_domain(uid: int) -> str:
    return f"gui/{uid}"


# meiva:note
# The directory the user's LaunchAgent plists live in.
def helper_job_agents_dir(user_home: Path) -> Path:
    return user_home / "Library" / "LaunchAgents"


def helper_job_plist_path(home: Path, *, facts: LaunchdFacts) -> Path:
    return facts.agents_dir / f"{helper_job_label(home)}.plist"


def helper_stop_intent(home: Path, *, uid: int) -> bool:
    """Read native shutdown intent; corrupt or foreign inputs are never absence."""
    path = home / "run" / "meiva-root" / "helper-stopped"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != uid
        or info.st_mode & 0o077
        or path.read_bytes() != b"stopped\n"
    ):
        raise RuntimeError("helper stop intent is unreadable or invalid; custody retained")
    return True


def clear_helper_stop_intent(home: Path, *, uid: int) -> None:
    """Explicit start only, after positive native job absence."""
    if not helper_stop_intent(home, uid=uid):
        return
    path = home / "run" / "meiva-root" / "helper-stopped"
    path.unlink()
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def retirement_query(
    target: str, deadline: float, *, run: BoundedRunner, timers: Timers
) -> str | None:
    """Absence is one positive launchd result; all other failures are unknown."""
    result = retirement_command(["print", target], deadline, run=run, timers=timers)
    if result.returncode == 113 and b"Could not find service" in result.stderr:
        return None
    if result.returncode:
        raise RuntimeError(f"cannot inspect helper job {target}: {result.stderr!r}")
    return result.stdout.decode()


def retirement_command(
    args: list[str], deadline: float, *, run: BoundedRunner, timers: Timers
) -> subprocess.CompletedProcess[bytes]:
    remaining = deadline - timers.monotonic()
    if remaining <= 0:
        raise TimeoutError("helper retirement deadline expired; registry must be retained")
    return run(["launchctl", *args], timeout=remaining, capture_output=True)


def helper_job_loaded(
    home: Path, *, facts: LaunchdFacts, run: BoundedRunner, timers: Timers
) -> bool:
    """Whether launchd holds this home's helper job; an unknown answer raises."""
    target = f"{facts.domain}/{helper_job_label(home)}"
    return (
        retirement_query(target, timers.monotonic() + _READ_TIMEOUT_S, run=run, timers=timers)
        is not None
    )


def require_retired_helper(
    executable: Path, *, home: Path, facts: LaunchdFacts, run: BoundedRunner, timers: Timers
) -> None:
    """Refuse replacing this home's installed helper executable while it may still run.

    The helper is meiva-root's parent, so its artifact is replaced only after
    ``meiva stop`` retired it: launchd holds no job under this home's label, the
    job's plist is gone, and no live process runs ``executable``. An unknown
    launchd answer raises from ``retirement_query``; it never reads as absence.
    """
    plist = helper_job_plist_path(home, facts=facts)
    if helper_job_loaded(home, facts=facts, run=run, timers=timers):
        blocker = f"launchd still has {facts.domain}/{helper_job_label(home)} loaded"
    elif plist.exists() or plist.is_symlink():
        blocker = f"its job definition {plist} is still registered"
    elif pids := executable_pids(executable):
        blocker = f"pid {', '.join(map(str, pids))} still runs {executable}"
    else:
        return
    raise RuntimeError(
        f"the installed permissions helper is stale but still in use ({blocker}); "
        "run `meiva stop` first (see `meiva stop --help`), then `meiva start` with this unit's explicit service selection replaces it"
    )


def executable_pids(executable: Path) -> list[int]:
    """Live processes whose image was loaded from the file at ``executable``.

    A process whose executable path the kernel does not report (another user's,
    a zombie's, or one whose image file was already unlinked) cannot be running
    the file now at that path, so replacing it cannot disturb that process.
    """
    target = executable.resolve()
    return sorted(
        process.pid
        for process in psutil.process_iter(["exe"])
        if process.info["exe"] and Path(process.info["exe"]).resolve() == target
    )


def retirement_plist(path: Path, home: Path, socket_path: Path, *, uid: int) -> tuple[bytes, str]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022:
        raise RuntimeError(f"helper plist is not an owned immutable input: {path}")
    original = path.read_bytes()
    raw: object = plistlib.loads(original)
    if not _is_str_dict(raw):
        raise TypeError(f"helper plist has invalid launch inputs: {path}")
    environment = raw["EnvironmentVariables"]
    argv = raw["ProgramArguments"]
    if not _is_str_dict(environment) or not _is_object_list(argv):
        raise TypeError(f"helper plist has invalid launch inputs: {path}")
    if (
        raw["Label"] != helper_job_label(home)
        or raw.get("KeepAlive") != {"SuccessfulExit": False}
        or environment.get("MEIVA_PERMISSIONS_HELPER_SOCKET") != str(socket_path)
        or environment.get("MEIVA_PERMISSIONS_HELPER_ROOT_SEED")
        != str(home / "run" / "meiva-root" / "seed.json")
        or len(argv) != 1
        or not isinstance(argv[0], str)
    ):
        raise RuntimeError(f"helper plist does not bind the exact home and shutdown policy: {path}")
    return original, argv[0]


def retirement_owner(
    state: str, socket_path: Path, executable: str, *, connect: HelperAtSocket, caller_pid: int
) -> OwnedProcess:
    match = re.search(r"^\s*pid = (\d+)\s*$", state, re.MULTILINE)
    if match is None:
        raise RuntimeError("loaded helper lacks a native owner; retirement requires reconciliation")
    owner = OwnedProcess.capture(psutil.Process(int(match[1])))
    if (
        not owner.live()
        or Path(psutil.Process(owner.pid).exe()).resolve() != Path(executable).resolve()
    ):
        raise RuntimeError("loaded helper executable does not match its home job")
    if leader_owns_pids(owner, {caller_pid}):
        raise RuntimeError("cannot unregister the caller's permissions-helper ancestor")
    reply = connect(socket_path).ping()
    if (
        reply.get("pid") != owner.pid
        or reply.get("root_stop_intent_v1") is not True
        or reply.get("helper_shutdown_v1") is not True
    ):
        raise RuntimeError("helper socket does not prove this native job and stop protocol")
    _require_empty_helper(owner, socket_path, connect=connect)
    return owner


def _require_empty_helper(
    owner: OwnedProcess, socket_path: Path, *, connect: HelperAtSocket
) -> None:
    client = connect(socket_path)
    root = client.root_status()
    stopped = root["state"] == "stopped" and root["stop_requested"] is True
    unseeded = root["state"] == "unseeded" and root["seeded"] is False
    if root.get("pid") is not None or not (stopped or unseeded):
        raise RuntimeError("helper still owns an active or uncertain root")
    if client.session_list() or capture_tree(owner) != {owner}:
        raise RuntimeError("helper still owns native children; stop them before unregistering")


def unregister_helper(
    home: Path,
    *,
    helper_port: int,
    connect: HelperAtSocket,
    facts: LaunchdFacts,
    caller_pid: int,
    run: BoundedRunner,
    timers: Timers,
    timeout_s: float = 30.0,
    force: bool = False,
) -> None:
    """Retire only this home's stopped helper; uncertainty retains its registry slot.

    The caller must first close root services and broker sessions. No foreign PID is signalled,
    no shared signed bundle is deleted, and a descendant cannot unload its ancestor.
    """
    if facts.platform != "darwin":
        return
    if not home.is_absolute() or home.resolve() != home or not 0 < helper_port < 65536:
        raise ValueError("helper retirement requires a canonical home and registered port")
    from base.native_process.root_control.instance_lock import (
        acquire_instance_lock,
        release_instance_lock,
    )

    run_dir = home / "run" / "meiva-root"
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    deadline = timers.monotonic() + timeout_s
    original = (
        None
        if force
        else _request_helper_shutdown(
            home,
            helper_port,
            deadline,
            connect,
            facts=facts,
            caller_pid=caller_pid,
            run=run,
            timers=timers,
        )
    )
    lock_fd = acquire_instance_lock(run_dir)
    try:
        _unregister_stopped_helper(
            home,
            helper_port,
            deadline,
            force=force,
            expected=original,
            connect=connect,
            facts=facts,
            caller_pid=caller_pid,
            run=run,
            timers=timers,
        )
    finally:
        release_instance_lock(lock_fd)


def _request_helper_shutdown(
    home: Path,
    port: int,
    deadline: float,
    connect: HelperAtSocket,
    *,
    facts: LaunchdFacts,
    caller_pid: int,
    run: BoundedRunner,
    timers: Timers,
) -> bytes | None:
    target = f"{facts.domain}/{helper_job_label(home)}"
    state = retirement_query(target, deadline, run=run, timers=timers)
    if state is None:
        return None
    path = helper_job_plist_path(home, facts=facts)
    socket_path = home / "run" / f"permissions-helper.{port}.sock"
    original, executable = retirement_plist(path, home, socket_path, uid=facts.uid)
    if _job_pid(state) is None:
        _require_idle_job(state, home, uid=facts.uid)
        return original
    owner = retirement_owner(state, socket_path, executable, connect=connect, caller_pid=caller_pid)
    if path.is_symlink() or path.read_bytes() != original:
        raise RuntimeError("helper definition changed before shutdown")
    reply = connect(socket_path).shutdown_helper(home / "run" / "meiva-root")
    if reply != {"stopping": True, "pid": owner.pid, "run_dir": str(home / "run" / "meiva-root")}:
        raise RuntimeError("helper shutdown did not acknowledge exact-home native custody")
    wait_retirement_owner(owner, deadline, timers=timers)
    while (latest := retirement_query(target, deadline, run=run, timers=timers)) is not None:
        if _job_pid(latest) is None:
            _require_idle_job(latest, home, uid=facts.uid)
            break
        if _job_pid(latest) != owner.pid:
            raise RuntimeError("helper relaunched after acknowledged shutdown; custody retained")
        timers.sleep(_remaining_sleep(deadline, timers))
    return original


def _require_idle_job(state: str, home: Path, *, uid: int) -> None:
    if (
        _job_pid(state) is not None
        or re.search(r"^\s*state = not running\s*$", state, re.MULTILINE) is None
        or re.search(r"^\s*last exit code = 0\s*$", state, re.MULTILINE) is None
        or not helper_stop_intent(home, uid=uid)
    ):
        raise RuntimeError("helper job is not positively idle with retained shutdown intent")


def _unregister_stopped_helper(
    home: Path,
    port: int,
    deadline: float,
    *,
    force: bool,
    expected: bytes | None,
    connect: HelperAtSocket,
    facts: LaunchdFacts,
    caller_pid: int,
    run: BoundedRunner,
    timers: Timers,
) -> None:
    target = f"{facts.domain}/{helper_job_label(home)}"
    path = helper_job_plist_path(home, facts=facts)
    state = retirement_query(target, deadline, run=run, timers=timers)
    socket_path = home / "run" / f"permissions-helper.{port}.sock"
    if state is None and not path.exists() and not path.is_symlink():
        _require_absent_socket(socket_path, deadline, timers=timers)
        return
    original, executable = retirement_plist(path, home, socket_path, uid=facts.uid)
    if expected is not None and original != expected:
        raise RuntimeError("helper definition changed during shutdown; preserving it")
    if state is None:
        # An earlier completed native stop may have retained the definition.
        # A socket still accepting connections contradicts that absence, even
        # when launchd no longer owns the listener. Never restart it to retire it.
        _require_absent_socket(socket_path, deadline, timers=timers)
        if retirement_query(target, deadline, run=run, timers=timers) is not None:
            raise RuntimeError("helper job appeared during retirement; preserving its definition")
        _unlink_unchanged_plist(path, original)
        return
    if _job_pid(state) is not None:
        if not force:
            raise RuntimeError("helper retirement still has a native owner; custody retained")
        owner = retirement_owner(
            state, socket_path, executable, connect=connect, caller_pid=caller_pid
        )
        latest = retirement_query(target, deadline, run=run, timers=timers)
        latest_pid = _job_pid(latest)
        if path.is_symlink() or path.read_bytes() != original or latest_pid != owner.pid:
            raise RuntimeError("helper job changed before retirement")
        # Explicit force revokes native restart authority before any escalation.
        _bootout_stopped_helper(target, deadline, run=run, timers=timers)
        force_retirement_owner(owner, deadline, timers=timers)
    else:
        _require_idle_job(state, home, uid=facts.uid)
        _require_absent_socket(socket_path, deadline, timers=timers)
        _bootout_stopped_helper(target, deadline, run=run, timers=timers)
    _unlink_unchanged_plist(path, original)


def _job_pid(state: str | None) -> int | None:
    match = None if state is None else re.search(r"^\s*pid = (\d+)\s*$", state, re.MULTILINE)
    return None if match is None or int(match[1]) <= 0 else int(match[1])


def force_retirement_owner(owner: OwnedProcess, deadline: float, *, timers: Timers) -> None:
    # A fresh native birth check is required at each signal, without the legacy
    # tolerance used for older persisted process records elsewhere.
    if deadline <= timers.monotonic():
        raise TimeoutError("helper retirement deadline expired; registry must be retained")
    try:
        process = psutil.Process(owner.pid)
        if OwnedProcess.capture(process) != owner:
            raise RuntimeError("helper native identity changed before stop")
        process.kill()
        process.wait(timeout=max(0.0, deadline - timers.monotonic()))
    except psutil.NoSuchProcess:
        return
    except psutil.TimeoutExpired as exc:
        raise TimeoutError("helper did not exit; definition and custody retained") from exc


def wait_retirement_owner(owner: OwnedProcess, deadline: float, *, timers: Timers) -> None:
    try:
        process = psutil.Process(owner.pid)
        if OwnedProcess.capture(process) != owner:
            return
        process.wait(timeout=max(0.0, deadline - timers.monotonic()))
    except psutil.NoSuchProcess:
        return
    except psutil.TimeoutExpired as exc:
        raise TimeoutError("helper shutdown did not complete; custody retained") from exc


def _bootout_stopped_helper(
    target: str, deadline: float, *, run: BoundedRunner, timers: Timers
) -> None:
    result = retirement_command(["bootout", target], deadline, run=run, timers=timers)
    if result.returncode:
        raise RuntimeError(f"helper bootout failed; custody retained: {result.stderr!r}")
    while retirement_query(target, deadline, run=run, timers=timers) is not None:
        timers.sleep(_remaining_sleep(deadline, timers))


def _require_absent_socket(path: Path, deadline: float, *, timers: Timers) -> None:
    remaining = deadline - timers.monotonic()
    if remaining <= 0:
        raise TimeoutError("helper retirement deadline expired; registry must be retained")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode):
        raise RuntimeError("cannot prove helper socket absence from a non-socket input")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(remaining)
        try:
            connection.connect(str(path))
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ECONNREFUSED):
                return
            raise RuntimeError("cannot prove helper socket absence; custody retained") from exc
    raise RuntimeError("helper socket remains live without native job custody")


def _unlink_unchanged_plist(path: Path, original: bytes) -> None:
    if path.is_symlink() or path.read_bytes() != original:
        raise RuntimeError("helper plist changed during retirement; preserving it")
    path.unlink()


def read_helper_job(*, home: Path, facts: LaunchdFacts, run: BoundedRunner) -> str | None:
    """One ``launchctl print`` dump of this cluster's helper job.

    None when launchd has no such job — and equally when the read itself could
    not run (a call the bound killed): every caller's next question is what the
    job state says, and a read that says nothing reads as an absent job.
    """
    cmd = ["launchctl", "print", f"{facts.domain}/{helper_job_label(home)}"]
    try:
        proc = run(cmd, timeout=_READ_TIMEOUT_S, capture_output=True)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode(errors="replace")


@dataclass(frozen=True, slots=True)
class HelperJobState:
    """The launchd facts the helper healthcheck classifies from."""

    job_state: str | None
    """``job state`` value — the line that carries ``spawn failed``."""

    last_exit_code: str | None
    """``last exit code`` value (e.g. ``78: EX_CONFIG``); None before any exit."""

    needs_lwcr_update: bool
    """Whether the dump carries the ``needs LWCR update`` properties marker."""

    btm_uuid: str | None
    """The job's recorded BTM uuid, when launchd shows one."""

    runs: int | None
    """Launchd's spawn count, when the dump shows one."""


_JOB_STATE_RE = re.compile(r"^\s*job state = (.+)$", re.MULTILINE)
_LAST_EXIT_RE = re.compile(r"^\s*last exit code = (.+)$", re.MULTILINE)
_BTM_UUID_RE = re.compile(r"^\s*BTM uuid = (.+)$", re.MULTILINE)
_RUNS_RE = re.compile(r"^\s*runs = (\d+)$", re.MULTILINE)

_NEEDS_LWCR_MARKER = "needs LWCR update"


def parse_job_state(text: str) -> HelperJobState:
    """Parse one ``launchctl print`` dump; unknown lines are ignored (pure)."""
    return HelperJobState(
        job_state=_capture(_JOB_STATE_RE, text),
        last_exit_code=_capture(_LAST_EXIT_RE, text),
        needs_lwcr_update=_NEEDS_LWCR_MARKER in text,
        btm_uuid=_capture(_BTM_UUID_RE, text),
        runs=_runs(_RUNS_RE, text),
    )


def _capture(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return None if match is None else match.group(1).strip()


def _runs(pattern: re.Pattern[str], text: str) -> int | None:
    match = pattern.search(text)
    return None if match is None else int(match.group(1))
