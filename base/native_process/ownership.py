# meiva:note
# PID/birth-validated process-tree ownership shared by the stop path and probes.
#
# The stop boundary (``cli.commands.lifecycle.stop.shell_sessions``) and the frontend identity
# probe (``services.supervision.healthchecks.frontend``) must answer the same question — "is
# this pid the recorded session's leader or one of its descendants, still the
# process it was?" — so the primitive lives here, importable by both without the
# ops layer reaching through services into cli (issue #2123).

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeGuard

import psutil

from base.native_process import pidfd
from base.native_process.birth_key import process_birth_key
from base.native_process.kernel_identity import native_boot_id, pid_starttime_ticks
from base.native_process.os_platform import is_macos

# Identity reads use the stable start timestamp (stable_create_time): on macOS
# the uncorrected kernel value, elsewhere the public create_time(). The public
# macOS value re-derives from the wall clock with a boot-time correction
# quantized to whole seconds (measured 2026-09-12: one live process read
# 1.000000s apart by two import epochs), and wall-clock steps move the
# Linux value too. Linux uses its starttime tick; other native birth readings
# compare exactly. Legacy records are not silently adopted across this boundary.


# How much of a process's command name a message or a log line shows. The name
# can come from the process itself: psutil extends Linux's 15-byte comm with
# argv[0]'s basename, which has no length limit.
_SHOWN_NAME_CHARS = 64


def shown_name(name: str) -> str:
    """A process-chosen command name as inert text: quoted, escaped and capped."""
    if len(name) <= _SHOWN_NAME_CHARS:
        return repr(name)
    return f"{name[:_SHOWN_NAME_CHARS]!r}..."


class _MonotonicCreateTimeProc(Protocol):
    def create_time(self, *, monotonic: bool = ...) -> float: ...


class _ProcessWithPlatformProc(Protocol):
    _proc: _MonotonicCreateTimeProc


def _has_platform_proc(process: object) -> TypeGuard[_ProcessWithPlatformProc]:
    return hasattr(process, "_proc")


def _sys_platform(platform: str | None = None) -> str:
    if platform is not None:
        return platform
    return "darwin" if is_macos() else "linux"


def stable_create_time(process: psutil.Process) -> float:
    """Native start timestamp; Linux uses start ticks for identity instead.

    psutil's public `create_time()` is not bit-stable across readers on macOS:
    it adds a whole-second wall-clock correction on top of the kernel value
    (and, since psutil 7.2, caches it per `Process` instance), so two readings
    of one live process taken by different import epochs disagree by exactly
    the correction. The uncorrected kernel value is what psutil itself keys
    pid reuse on — `Process._proc.create_time(monotonic=True)`, documented
    "stable over changes to system time" — and identity records must be
    written and compared through that same value. Linux's public value includes
    a reconstructed boot wall time and is diagnostic, never its identity key.
    """
    if _sys_platform() == "darwin":
        # psutil's per-platform object is untyped in the public stubs; its
        # monotonic create_time is the pid-reuse key used above.
        if not _has_platform_proc(process):
            raise RuntimeError(f"psutil.Process lacks _proc for PID {process.pid}")
        return float(process._proc.create_time(monotonic=True))
    return process.create_time()


def _validate_start_ticks(pid: int, ticks: int | None) -> int | None:
    if ticks is not None and (type(ticks) is not int or ticks <= 0):
        raise RuntimeError(f"invalid Linux start ticks for PID {pid}")
    return ticks


def _start_ticks(pid: int) -> int | None:
    return _validate_start_ticks(pid, pid_starttime_ticks(pid))


# meiva:note
# What one look at a pid saw (`OwnedProcess.read_live`): the Linux start ticks as read, whether the
# pid is still in the process table, the psutil status, and the identity captured now when the
# record carries no ticks.
@dataclass(frozen=True)
class LiveReading:
    ticks: int | None
    pid_exists: bool
    status: str
    identity: OwnedProcess | None


@dataclass(frozen=True)
class OwnedProcess:
    pid: int
    birth: float
    starttime: int | None

    @classmethod
    def capture(
        cls,
        process: psutil.Process,
        *,
        platform: str | None = None,
        observed_ticks: Sequence[int | None] | None = None,
    ) -> OwnedProcess:
        """Sample the PID's current native birth; lineage needs separate evidence."""
        sys_platform = _sys_platform(platform)
        first_raw: int | None = None
        if observed_ticks is not None:
            first_raw = observed_ticks[0] if len(observed_ticks) >= 1 else None
            ticks = _validate_start_ticks(process.pid, first_raw)
        else:
            ticks = _start_ticks(process.pid)
        identity = cls(process.pid, stable_create_time(process), ticks)
        if sys_platform == "linux" and identity.starttime is None:
            if not psutil.pid_exists(process.pid):
                raise psutil.NoSuchProcess(process.pid)
            raise RuntimeError(f"cannot capture Linux start ticks for PID {process.pid}")
        if ticks is not None:
            if observed_ticks is not None:
                second_raw = observed_ticks[1] if len(observed_ticks) >= 2 else first_raw
                after = _validate_start_ticks(process.pid, second_raw)
            else:
                after = _start_ticks(process.pid)
            if after is None and psutil.pid_exists(process.pid):
                raise RuntimeError(f"cannot finish Linux start ticks capture for PID {process.pid}")
            if after != ticks:
                raise psutil.NoSuchProcess(process.pid, msg="process changed during native capture")
        return identity

    def birth_key(self, *, platform: str | None = None) -> tuple[int, str, int | float]:
        """Index independently observed births without altering receipt equality."""
        return process_birth_key(
            self.pid,
            self.birth,
            self.starttime,
            platform=_sys_platform(platform),
        )

    def same_birth(self, other: OwnedProcess, *, platform: str | None = None) -> bool:
        """Compare captured native identities without a wall-clock fallback for ticks."""
        sys_platform = _sys_platform(platform)
        if sys_platform == "linux" and (self.starttime is None or other.starttime is None):
            return False
        return self.birth_key(platform=sys_platform) == other.birth_key(platform=sys_platform)

    def birth_matches(self, process: psutil.Process) -> bool:
        """Whether a fresh observation still identifies this captured birth."""
        return self.same_birth(OwnedProcess.capture(process))

    # meiva:note
    # Whether a start-tick read still names this recorded process.
    #
    # `observed` is the pid's start ticks as read now (`None` when the read
    # found no /proc entry) and `pid_exists` whether the pid is still in the
    # process table. A tracked process exiting and being reaped exactly between
    # psutil's eager validation and the raw start-time read leaves that read
    # without a /proc entry — that IS the exit, not an unverifiable identity: a
    # vanished pid converges as gone (the 2026-09-20 wave-2 window raised out of
    # a stop this way and aborted the cluster update with the tracked daemon
    # already exiting). Only a pid that still exists while its start time cannot
    # be read keeps the loud error.
    def ticks_match(self, observed: int | None, *, pid_exists: bool) -> bool:
        if self.starttime is None:
            raise ValueError(f"PID {self.pid} has no recorded start ticks to compare")
        if observed is None:
            if not pid_exists:
                return False
            raise RuntimeError(f"cannot verify process identity for PID {self.pid}")
        return observed == self.starttime

    # meiva:note
    # Look at this record's pid once: the start ticks as read (Linux), whether the pid is still in
    # the process table, its status, and, where the record carries no ticks, the identity the
    # process has now. None when there is no such process. `judge_live` decides from the reading.
    def read_live(
        self,
        *,
        platform: str | None = None,
        observed_ticks: Sequence[int | None] | None = None,
    ) -> LiveReading | None:
        sys_platform = _sys_platform(platform)
        try:
            process = psutil.Process(self.pid)
            if self.starttime is None:
                if sys_platform == "linux":
                    raise RuntimeError(f"missing Linux start ticks for PID {self.pid}")
                return LiveReading(
                    ticks=None,
                    pid_exists=True,
                    status=process.status(),
                    identity=OwnedProcess.capture(process, platform=sys_platform),
                )
            if observed_ticks is not None:
                raw = observed_ticks[0] if len(observed_ticks) >= 1 else None
                observed = _validate_start_ticks(self.pid, raw)
            else:
                observed = _start_ticks(self.pid)
            # meiva:note
            # Existence is asked only when the read found nothing.
            exists = observed is not None or psutil.pid_exists(self.pid)
            return LiveReading(
                ticks=observed, pid_exists=exists, status=process.status(), identity=None
            )
        except psutil.NoSuchProcess:
            return None

    # meiva:note
    # Whether a reading still names this recorded process and it is neither a zombie nor dead.
    def judge_live(self, reading: LiveReading | None) -> bool:
        if reading is None:
            return False
        if self.starttime is None:
            if reading.identity is None or not self.same_birth(reading.identity):
                return False
        elif not self.ticks_match(reading.ticks, pid_exists=reading.pid_exists):
            return False
        return reading.status not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)

    # meiva:note
    # Whether the pid still is this recorded process.
    def live(
        self,
        *,
        platform: str | None = None,
        observed_ticks: Sequence[int | None] | None = None,
    ) -> bool:
        return self.judge_live(self.read_live(platform=platform, observed_ticks=observed_ticks))

    def send_signal(self, signum: int) -> bool:
        """Signal this birth, retaining the Linux process object through delivery.

        A gone birth returns false. Unknown or changed custody refuses. Linux
        never falls back to a numeric PID signal if pidfd custody is unavailable.
        """
        if not self.live():
            return False
        return self.signal_pinned(signum)

    # meiva:note
    # Pin and signal this PID after a liveness check, refusing if its birth changed.
    def signal_pinned(self, signum: int) -> bool:
        descriptor: int | None = None
        try:
            if _sys_platform() == "linux":
                descriptor = pidfd.open_process(self.pid)
            process = psutil.Process(self.pid)
            if not self.birth_matches(process):
                raise RuntimeError(f"native identity changed before signal: {self.pid}")
            if descriptor is not None:
                pidfd.send_signal(descriptor, signum)
            else:
                process.send_signal(signum)
            return True
        except (ProcessLookupError, psutil.NoSuchProcess):
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _parent_edge(identity: OwnedProcess) -> tuple[OwnedProcess, int] | None:
    """Read one fresh parent link bracketed by the child's native generation."""
    process = psutil.Process(identity.pid)
    if not identity.birth_matches(process):
        return None
    parent_pid = process.ppid()
    if not identity.birth_matches(psutil.Process(identity.pid)):
        return None
    return identity, parent_pid


def is_descendant(identity: OwnedProcess, ancestor: OwnedProcess) -> bool:
    """Prove a current native ancestry chain, without psutil's cached PID map.

    Each edge is checked again after walking to the retained ancestor. A reused
    intermediate PID or reparented child invalidates the chain. This samples
    membership; it does not grant custody over later births at those PIDs.
    """
    edges: list[tuple[OwnedProcess, int]] = []
    seen: set[int] = set()
    current = identity
    try:
        while not current.same_birth(ancestor):
            if current.pid in seen:
                return False
            seen.add(current.pid)
            edge = _parent_edge(current)
            if edge is None or edge[1] <= 0:
                return False
            edges.append(edge)
            current = OwnedProcess.capture(psutil.Process(edge[1]))
        return ancestor.live() and all(_parent_edge(edge[0]) == edge for edge in edges)
    except psutil.NoSuchProcess:
        return False


def capture_tree(
    identity: OwnedProcess,
    *,
    candidate_children: Sequence[psutil.Process] | None = None,
) -> set[OwnedProcess]:
    """Capture only generation- and ancestry-validated members of this tree."""
    if not identity.live():
        return set()
    try:
        children = (
            psutil.Process(identity.pid).children(recursive=True)
            if candidate_children is None
            else candidate_children
        )
        captured = {identity}
        for child in children:
            try:
                # children() is a PID-map hint, not membership authority. Its
                # Process may be stale, or already describe a reused PID.
                member = OwnedProcess.capture(psutil.Process(child.pid))
                if is_descendant(member, identity):
                    captured.add(member)
            except psutil.NoSuchProcess:
                continue
        # A PID replacement during enumeration invalidates the capture. Never
        # attach a replacement process's descendants to the original identity.
        if not identity.live():
            try:
                current = OwnedProcess.capture(psutil.Process(identity.pid))
            except psutil.NoSuchProcess:
                return captured
            if current.live():
                raise RuntimeError(f"process changed during descendant capture: {identity.pid}")
        return captured
    except psutil.NoSuchProcess:
        return {identity}


def retain_processes(
    retained: set[OwnedProcess],
    observed: Iterable[OwnedProcess],
    *,
    platform: str | None = None,
) -> None:
    """Add new native births, preserving the original receipt for every known one."""
    keys = {identity.birth_key(platform=platform) for identity in retained}
    for identity in observed:
        key = identity.birth_key(platform=platform)
        if key not in keys:
            retained.add(identity)
            keys.add(key)


def leader_owns_pids(leader: OwnedProcess, pids: set[int]) -> bool:
    """Whether any pid in `pids` is `leader` or a birth-validated descendant.

    False when the leader is gone: a descendant whose leader died carries no
    proof of whose it is (the stop path converges such survivors through the
    recorded process group, a stronger claim than this probe). The identity
    question is the same whether the leader came from a session record or, on
    a root-driven host, from a tree unit's row (task #3370) — only the source
    of `leader` differs, never this rule.
    """
    if not leader.live():
        return False
    owned = {identity.pid for identity in capture_tree(leader)}
    return bool(owned & pids)


def format_process_node_facts(
    *,
    pid: int,
    name: str,
    executable: str,
    created_at: float,
    starttime: int | None,
    boot_id: str | None,
    parent_pid: int,
    cmdline: Sequence[str] = (),
) -> dict[str, object]:
    facts: dict[str, object] = {
        "pid": pid,
        "name": name,
        "executable": executable,
        "created_at": created_at,
        "starttime": starttime,
        "boot_id": boot_id,
        "parent_pid": parent_pid,
    }
    if name == "node" and len(cmdline) > 1:
        # meiva:note
        # A Node controller (DeepSeek Harness) is identified by its script, not its interpreter; only argv[1] is recorded.
        facts["script"] = cmdline[1]
    return facts


def assemble_process_metadata(
    *,
    pid: int,
    nodes: Sequence[dict[str, object]],
    codex_home: str | None = None,
    observation_error: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {"pid": pid, "ancestors": []}
    if codex_home:
        result["codex_home"] = codex_home
    ancestors: list[dict[str, object]] = []
    for depth, facts in enumerate(nodes):
        if depth == 0:
            result.update(facts)
        else:
            ancestors.append(facts)
    result["ancestors"] = ancestors
    if observation_error is not None:
        result["observation_error"] = observation_error
    return result


# meiva:note
# Observed process facts for one caller, standardized for attestation.
#
# `codex_home` is the `CODEX_HOME` the caller's invocation saw (provider routing context the
# relay spec needs, never an executor identity assertion); the caller's root reads it.
#
# Identity fields use the stable start time (`stable_create_time`) so a
# later reader compares against the same value the kernel keeps. The parent
# walk is bounded and records what it could observe instead of failing: a
# permission gap on an ancestor path must not lose the facts below it.
def process_metadata(*, codex_home: str | None) -> dict[str, Any]:
    nodes: list[dict[str, object]] = []
    observation_error: str | None = None
    process = psutil.Process()
    caller_pid = process.pid
    for _depth in range(8):
        try:
            identity = OwnedProcess.capture(process)
            name = process.name()
            cmdline = process.cmdline() if name == "node" else ()
            nodes.append(
                format_process_node_facts(
                    pid=process.pid,
                    name=name,
                    executable=process.exe(),
                    created_at=identity.birth,
                    starttime=identity.starttime,
                    boot_id=native_boot_id(),
                    parent_pid=process.ppid(),
                    cmdline=cmdline,
                )
            )
            parent = process.parent()
            if parent is None:
                break
            process = parent
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            observation_error = type(exc).__name__
            break
    return assemble_process_metadata(
        pid=caller_pid,
        nodes=nodes,
        codex_home=codex_home,
        observation_error=observation_error,
    )
