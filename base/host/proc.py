"""Local OS process primitives: the liveness probe, the force-kill, and the
timeout that actually bounds the work (`run_bounded`).

`process_alive` and `process_cmdline` inspect local processes. A PID from
another machine is not ours to probe, and PID liveness alone is not identity:
callers that act on an execution owner must also verify its OS birth evidence.

`run_bounded` is the module's other half and exists because
`subprocess.run(..., timeout=T)` does not do what its name implies: on expiry
Python kills the **one** process it spawned and leaves every descendant running.
Measured consequence: orphaned `git` + `ssh` + `sh` processes, because a
timeout kills the launcher and the process tail below it survives. Every
Meiva-initiated subprocess with a timeout should go through `run_bounded`
instead, so the bound applies to the work rather than to whichever wrapper
happened to be on top.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import IO, Literal, Protocol, TypedDict, TypeGuard, Unpack, overload

import psutil

from base.native_process.ownership import OwnedProcess, capture_tree

__all__ = [
    "CommandRunner",
    "SubprocessError",
    "TreeCapture",
    "run_bounded",
    "terminate_owned_tree",
]

SubprocessError = subprocess.SubprocessError

# psutil exceptions that mean "the process is already gone / not ours to touch" —
# expected during a teardown, not an error: between enumerating a tree and
# signalling it, any member may exit on its own.
_GONE = (psutil.NoSuchProcess, psutil.AccessDenied, OSError)

# How long the tree gets between the terminate and the kill. This is NOT
# politeness — the caller's `timeout` already was all the patience the work
# gets. It buys exactly one thing: git installs signal handlers that unlink its
# lockfiles (`.git/index.lock`), so a SIGTERM'd `git pull` leaves a usable repo
# where a SIGKILL'd one can leave a lock that breaks the next git call. Seconds,
# not tens of seconds, because the thing we are killing has already proven it
# does not finish.
_TERMINATE_GRACE_S = 3.0

# Ceiling on the post-kill reap. A process that survives SIGKILL is in
# uninterruptible sleep (wedged NFS / a stuck driver) and no amount of waiting
# will collect it; we log nothing and return rather than hang the caller.
_REAP_TIMEOUT_S = 5.0

# Ceiling on draining the pipes after the tree is dead. The descendants
# inherited the write ends, so an un-killable holder would make an unbounded
# drain the new hang — the whole defect one layer down.
_DRAIN_TIMEOUT_S = 5.0

# meiva:note
# The entry module an agent exec-domain session leader runs. The exec spawn
# makes its root the session leader (`start_new_session`) and runs
# `agent.execution.child` directly (`agent/graph/exec/_subprocess.py::spawn_exec_child`).
# Compared as whole argv elements: the token is one exact argument, never a substring.
EXEC_DOMAIN_SESSION_ENTRIES = frozenset({"agent.execution.child"})


# meiva:note
# The agent exec domain this process runs inside — the entry module of this
# process's session leader (`agent.execution.child`), or
# None when this process is not in an exec-domain session.
#
# Membership, not ancestry: the exec root calls setsid (its spawn passes
# `start_new_session`), and `ExecProcessDomain.kill_and_reap()` SIGKILLs its process
# group as the tool call returns, while `sh -c "nohup meiva … &"` reparents the
# child out of any covered lineage WITHOUT leaving the session — the
# 2026-09-12 stranding: a restart that passed the ancestry guard was
# group-killed mid-drain and left the host paused. The session id is the
# widest cheap probe: every member still shares it after reparenting, and a
# member that re-arranged its process group (interactive job control) is
# refused too — deliberately, because the leg is still tied to an ephemeral
# call, not only to the kill that call performs. The env marker is not usable
# for this: `cli.main` clears `MEIVA_PROCESS_PROFILE` before any dispatch
# (`docs/decisions/runtime/config/2026-08-24-cli-full-settings-profile.md`) and session shells
# carry the exec request/result files and agent id as well, so only the
# session's identity discriminates (see `hosting_supervised_session` for the
# hosted-service half of the same refusal).
#
# A session whose leader already exited reads as None — the argv evidence is
# gone with it, and the domain's own teardown is already in flight by then.
def hosting_exec_domain() -> str | None:
    cmdline = process_cmdline(os.getsid(0))
    if cmdline is None:
        return None
    for argument in cmdline:
        if argument in EXEC_DOMAIN_SESSION_ENTRIES:
            return argument
    return None


def process_alive(pid: int) -> bool:
    """Liveness probe.

    True if `pid` names a live process; False only on an unambiguous
    "no such pid". A PermissionError — the pid was recycled and is now owned
    by another user — counts as alive: we must not declare a row an orphan
    when its pid maps to *some* running process.

    Uses `os.kill(pid, 0)` (signal 0 — existence test, no signal sent).
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def process_cmdline(pid: int) -> list[str] | None:
    """The OS-level argv `pid` was exec'd with, or None if it cannot be read.

    None means "no answer", never "no arguments". A pid that is gone, one whose
    argv this user may not read (a recycled pid now owned by someone else), and a
    zombie (whose argv the kernel has already released, surfaced as an empty
    list) all collapse to None, because none of them is evidence about *whose*
    process the pid is. A caller that must tell "gone" from "alive but opaque"
    pairs this with `process_alive` — the pair is what `ops.agent_identity` turns
    into a verdict.

    psutil reads the process table directly and delivers nothing to the target.
    """
    try:
        cmdline = psutil.Process(pid).cmdline()
    except (psutil.Error, OSError):
        return None
    return cmdline or None


ChildState = Literal["attached", "detached", "missing", "unverifiable"]


# meiva:note
# What one read of a process's table entry answered: its status and its parent id.
@dataclass(frozen=True)
class ProcessObservation:
    status: str
    ppid: int


# meiva:note
# One read of `pid`'s process entry, raising as psutil does: `psutil.NoSuchProcess`
# for a pid that names nothing, another `psutil.Error` (or an `OSError`) when the
# kernel refuses the read.
ProcessReader = Callable[[int], ProcessObservation]


# meiva:note
# The psutil read: the system implementation of `ProcessReader`. A caller that
# decides on the answer takes the reader as a parameter, so a test passes the
# answer — or the failure — it needs. A read the kernel really refuses is not
# something a live child can be asked to produce.
def read_process(pid: int) -> ProcessObservation:
    proc = psutil.Process(pid)
    return ProcessObservation(status=proc.status(), ppid=proc.ppid())


# meiva:note
# How live process `pid` reads relative to its expected chain parent.
#
# The root supervisor's self-check probes each managed child with this:
# ``attached`` — a genuinely live (zombie != live, the reaper's own rule)
# direct child of `parent_pid`; ``detached`` — live but parented elsewhere
# (reparented out of the chain, or the pid was recycled); ``missing`` — no
# such process, or a corpse awaiting its reaper (neither can serve);
# ``unverifiable`` — the read itself failed; the answer is never guessed.
def child_state(pid: int, parent_pid: int, *, read: ProcessReader) -> ChildState:
    try:
        observed = read(pid)
    except psutil.NoSuchProcess:
        return "missing"
    except (psutil.Error, OSError):
        return "unverifiable"
    if observed.status == psutil.STATUS_ZOMBIE:
        return "missing"
    return "attached" if observed.ppid == parent_pid else "detached"


def force_kill(pid: int) -> None:
    """Force-terminate `pid` (the SIGKILL intent), tolerant of an absent pid.

    A dead/absent pid is a silent no-op —
    callers force-kill exactly to make a row reach 'terminated', so racing the
    process's own exit must not raise.

    A pid this user may not signal is a no-op too, and for the same reason as
    `process_alive` reading it as alive: "could not deliver" is not "did not need
    to". The caller re-probes and reports the process as a survivor if delivery
    failed, so an unhandled exception must not interrupt that verification.
    """
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def request_stop(pid: int) -> None:
    """Ask `pid` to stop (the SIGTERM intent), tolerant of an absent pid.

    Complements `process_alive` and `force_kill` for callers that first request
    a graceful stop before escalating to SIGKILL.

    A pid this user may not signal returns quietly, like `force_kill` and for the
    same reason (see there). The realistic shape is not exotic: a stray from a
    `sudo`-run instance of this very checkout passes the caller's cmdline ownership
    check, `process_alive` correctly reads it as alive, and the signal is refused.
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return


def wait_owned(members: list[OwnedProcess], timeout: float) -> list[OwnedProcess]:
    """Bounded native exit observation; a permission gap remains a survivor."""
    clock = time.monotonic
    deadline = clock() + timeout
    while True:
        alive: list[OwnedProcess] = []
        for identity in members:
            try:
                if identity.live():
                    alive.append(identity)
            except (psutil.AccessDenied, OSError):
                alive.append(identity)
        remaining = deadline - clock()
        if not alive or remaining <= 0:
            return alive
        members = alive
        time.sleep(min(0.02, remaining))


# meiva:note
# Capture the generation- and ancestry-validated members of a process tree.
class TreeCapture(Protocol):
    def __call__(self, identity: OwnedProcess, /) -> set[OwnedProcess]: ...


def kill_process_tree(
    pid: int,
    *,
    grace_s: float = _TERMINATE_GRACE_S,
    include_root: bool = True,
    tree_capture: TreeCapture = capture_tree,
) -> None:
    """Take down `pid` **and every descendant**: terminate the tree, wait up to
    `grace_s`, then hard-kill whatever is still standing. A pid that is already
    gone is a no-op.

    With ``include_root=False``, capture and terminate only descendants. This
    lets a hard-exiting owner clean up its children without signalling itself
    or its process group (which may also contain its PTY shell).

    Escalation (terminate → wait → kill) rather than a straight kill for one
    reason only, spelled out at `_TERMINATE_GRACE_S`: git unlinks its lockfiles
    from a signal handler. Nothing here waits on the tree's cooperation — the
    kill is attempted after `grace_s` for surviving identities.

    The descendant set is enumerated **once, up front, while the parent is still
    alive**. Walking down from a dead parent is not possible: psutil resolves
    children by ppid, so the link is lost after reparenting. Only the root is
    signalled last when included.

    Limitation worth knowing: a descendant that has already double-forked away
    (reparented to init) is not in the ppid walk and is not reached. git and ssh
    do not do that, so the measured leak shape is covered; a process supervisor
    is not something to bound with a timeout in the first place. We also
    conservatively skip members whose identity or ancestry no longer matches
    before the first signal pass. Birth identity is rechecked before KILL;
    reparenting caused by our own TERM pass does not exempt survivors.
    """
    try:
        parent = OwnedProcess.capture(psutil.Process(pid))
        members = list(tree_capture(parent) - {parent})
        if include_root:
            members.append(parent)
    except psutil.NoSuchProcess:
        return

    terminate_owned_tree(members, grace_s=grace_s)


# meiva:note
# Terminate captured tree `members`, wait up to `grace_s`, then hard-kill survivors.
def terminate_owned_tree(
    members: Sequence[OwnedProcess], *, grace_s: float = _TERMINATE_GRACE_S
) -> None:
    for identity in members:
        # meiva:note
        # A member that exited between enumeration and this line is the normal
        # case, not a failure — that race is the whole reason the set is
        # snapshotted rather than re-walked.
        with contextlib.suppress(*_GONE):
            identity.send_signal(signal.SIGTERM)
    alive = wait_owned(list(members), grace_s)
    if not alive:
        return
    for identity in alive:
        with contextlib.suppress(*_GONE):
            identity.send_signal(signal.SIGKILL)
    wait_owned(alive, _REAP_TIMEOUT_S)


# meiva:note
# `run_bounded`'s call shape: argv, a required timeout, one `CompletedProcess` back. `text=True` answers
# `str`, no `text` answers `bytes`, as `run_bounded` does. It never raises for a non-zero exit. A tree
# that outlives `timeout` raises `subprocess.TimeoutExpired`, and a program that cannot start raises
# `OSError`. A component that must be testable takes one of these (inside a `Host`) and a test passes a
# fake; a fake that answers in one shape wraps itself with
# `base.host.system.tests.fake_commands.runner_from`. `input` mirrors `subprocess.run`'s own name.
class PopenOptions(TypedDict, total=False):
    cwd: str | os.PathLike[str] | None
    env: Mapping[str, str] | None
    stdin: int | IO[str] | IO[bytes] | None
    stdout: int | IO[str] | IO[bytes] | None
    stderr: int | IO[str] | IO[bytes] | None
    start_new_session: bool
    close_fds: bool
    shell: bool
    encoding: str
    errors: str
    universal_newlines: bool


class CommandRunner(Protocol):
    @overload
    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        capture_output: bool = False,
        input: bytes | str | None = None,
        text: Literal[True],
        **popen_kwargs: Unpack[PopenOptions],
    ) -> subprocess.CompletedProcess[str]: ...

    @overload
    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        capture_output: bool = False,
        input: bytes | str | None = None,
        text: Literal[False] | None = None,
        **popen_kwargs: Unpack[PopenOptions],
    ) -> subprocess.CompletedProcess[bytes]: ...


@overload
def run_bounded(
    argv: Sequence[str],
    *,
    timeout: float,
    capture_output: bool = False,
    input: bytes | str | None = None,
    text: Literal[True],
    **popen_kwargs: Unpack[PopenOptions],
) -> subprocess.CompletedProcess[str]: ...


@overload
def run_bounded(
    argv: Sequence[str],
    *,
    timeout: float,
    capture_output: bool = False,
    input: bytes | str | None = None,
    text: Literal[False] | None = None,
    **popen_kwargs: Unpack[PopenOptions],
) -> subprocess.CompletedProcess[bytes]: ...


# meiva:note
# `subprocess.run` whose timeout bounds the **work**, not just the process Python spawned: on expiry the
# whole tree dies (`kill_process_tree`) before `TimeoutExpired` propagates.
#
# Drop-in for the `subprocess.run(argv, timeout=..., check=False)` shape: `capture_output` and `input`
# are honoured, everything else is passed through to `Popen` (`cwd`, `env`, ...). There is deliberately
# no `check=`: this returns the `CompletedProcess` and the caller reads `returncode`, so a non-zero exit
# can never be confused with the timeout path. `TimeoutExpired` is raised exactly as `subprocess.run`
# raises it, carrying whatever output was captured before the bound tripped: a caller that treats a
# timeout as a failed fetch keeps working unchanged. This fixes the leak, not the control flow. The tree
# is dead by the time `TimeoutExpired` is raised.
#
# The no-input call shape stays exactly `communicate(timeout=timeout)` for callers that never asked to
# feed stdin. After a timeout the tree is killed and the pipes drained: their write ends were inherited
# by the descendants, so the drain only terminates because they are dead, and it is bounded anyway,
# because an un-killable holder would otherwise make the drain the new unbounded wait (the same defect,
# one layer down). `TimeoutExpired` already carries whatever was buffered before the bound tripped, so a
# drain that times out keeps that partial output; its `stdout` is typed `bytes | None` though a
# `text=True` run carries str, hence `setattr`. A `KeyboardInterrupt` or cancellation must not leak a
# tree either, and `Popen` still owns reaping. The argv is list-form and callers pass fixed argv. The
# `Popen` keywords the callers use are typed by `PopenOptions`; `text` is passed as the `bool | None`
# it is, which lands on typeshed's `Popen[Any]` fallback overload, so the streams are checked again
# in `_completed` and `communicate` goes through `_erased`.
def run_bounded(
    argv: Sequence[str],
    *,
    timeout: float,
    capture_output: bool = False,
    input: bytes | str | None = None,
    text: bool | None = None,
    **popen_kwargs: Unpack[PopenOptions],
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    options = _spawn_options(
        popen_kwargs, text=text, capture_output=capture_output, has_input=input is not None
    )
    proc = subprocess.Popen(argv, text=text, **options)  # noqa: S603
    communicate = _erased(proc.communicate)
    try:
        communicated = (
            communicate(timeout=timeout) if input is None else communicate(input, timeout=timeout)
        )
    except subprocess.TimeoutExpired as exc:
        kill_process_tree(proc.pid)
        with contextlib.suppress(subprocess.TimeoutExpired):
            drained = communicate(timeout=_DRAIN_TIMEOUT_S)
            if _is_pair(drained):
                kind = _text_stream if text else _bytes_stream
                setattr(exc, "stdout", kind(drained[0]))  # noqa: B010
                setattr(exc, "stderr", kind(drained[1]))  # noqa: B010
        raise
    except BaseException:
        kill_process_tree(proc.pid)
        proc.poll()
        raise
    return _completed(argv, proc.returncode, communicated, text=bool(text))


# meiva:note
# The validation `run_bounded` owes before it spawns, and the options `Popen` is given: the text-mode
# options need `text=True` (they are how `str` output is asked for, while the overloads promise `bytes`
# otherwise), and `capture_output` and `input` are exclusive with the pipes they imply.
def _spawn_options(
    popen_kwargs: PopenOptions, *, text: bool | None, capture_output: bool, has_input: bool
) -> PopenOptions:
    for other_text_option in ("encoding", "errors", "universal_newlines"):
        if other_text_option in popen_kwargs and not text:
            raise ValueError(
                f"{other_text_option}= needs text=True (it is how str output is asked for)"
            )
    options = PopenOptions(**popen_kwargs)
    if capture_output:
        if "stdout" in options or "stderr" in options:
            raise ValueError("capture_output=True is exclusive with stdout / stderr")
        options["stdout"] = subprocess.PIPE
        options["stderr"] = subprocess.PIPE
    if has_input:
        if options.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used")
        options["stdin"] = subprocess.PIPE
    return options


# meiva:note
# The `CompletedProcess` of a finished child from what `communicate` answered, with each stream checked
# to be the kind (`str` under `text=True`, else `bytes`) the overloads promised the caller.
def _completed(
    argv: Sequence[str], returncode: int, communicated: object, *, text: bool
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    if not _is_pair(communicated):
        raise TypeError(f"Popen.communicate answered {type(communicated).__name__}, not a pair")
    stdout, stderr = communicated
    if text:
        return subprocess.CompletedProcess[str](
            argv, returncode, _text_stream(stdout), _text_stream(stderr)
        )
    return subprocess.CompletedProcess[bytes](
        argv, returncode, _bytes_stream(stdout), _bytes_stream(stderr)
    )


# meiva:note
# A bound method with its signature erased, so a call is checked against nothing: `Popen.communicate`
# takes `str` or `bytes` input by the mode the child was opened in, which the type of `proc` (a
# union of both modes here) cannot say. The mode is `text`, which `_completed` checks on the way out.
def _erased(function: Callable[..., object]) -> Callable[..., object]:
    return function


def _is_object_tuple(value: object) -> TypeGuard[tuple[object, ...]]:
    return isinstance(value, tuple)


def _is_pair(value: object) -> TypeGuard[tuple[object, object]]:
    return _is_object_tuple(value) and len(value) == 2


# meiva:note
# One captured stream as text under `text=True`, or None when the child's pipe was not captured.
# `Popen.communicate` types its answer `Any`; checking the kind here is what lets the overloads above
# promise it to the caller.
def _text_stream(stream: object) -> str | None:
    if stream is None or isinstance(stream, str):
        return stream
    raise TypeError(f"expected a str stream, got {type(stream).__name__}")


# meiva:note
# One captured stream as bytes (the default), or None when the child's pipe was not captured.
def _bytes_stream(stream: object) -> bytes | None:
    if stream is None or isinstance(stream, bytes):
        return stream
    raise TypeError(f"expected a bytes stream, got {type(stream).__name__}")


def timeout_stderr_tail(exc: subprocess.TimeoutExpired, *, lines: int = 3) -> str:
    """The last `lines` non-empty stderr lines a timed-out child wrote, as text.

    A `run_bounded` timeout's `TimeoutExpired` carries whatever the child wrote
    before the bound tripped (the pipes are drained after the tree kill) — the
    "where was it when it died" evidence a bare timeout message drops. Bytes or
    None are normalized so callers can log/return the tail without type games:
    a fetch killed before ssh/git printed anything yields an empty string, which
    is itself the evidence that it died in the local/connect phase rather than
    mid-transfer (2026-08-27 fetch forensics).
    """
    raw: str | bytes | None = exc.stderr
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return " | ".join((raw or "").strip().splitlines()[-lines:])
