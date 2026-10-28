"""Durable custody for the home's directly launched PostgreSQL postmaster.

A pidfile describes PostgreSQL, but cannot grant native process authority. Only
this launch boundary records the exact child birth. Interrupted admission stays
unknown; ordinary observation never adopts a process from a pidfile.
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
from collections.abc import Callable, Generator, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any, Literal, Protocol

import psutil
from pydantic import Field, model_validator

from base.clock import Timers
from base.cluster.port_preflight import require_listener
from base.host.atomic_io import write_text_atomic
from base.host.verified_file.reader import regular_bytes
from base.log import logger
from base.native_process.evidence import EvidenceModel, ExpectedProcess
from base.native_process.kernel_identity import native_boot_id
from base.native_process.os_platform import Platform, file_lock
from base.native_process.ownership import OwnedProcess, capture_tree, retain_processes
from base.paths.private_dir import ensure_private_dir


class Receipt(EvidenceModel):
    version: Literal[1] = 1
    data: str
    directory: tuple[int, int]
    port: int = Field(gt=0, lt=65536)
    platform: Literal["linux", "darwin"]
    boot_id: str = Field(min_length=1)
    state: Literal["pending", "captured", "ready", "not-started"]
    owner: ExpectedProcess | None = None
    pidfile: tuple[int, int] | None = None
    header: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def coherent(self) -> Receipt:
        if (self.owner is not None) != (self.state in {"captured", "ready"}):
            raise ValueError("PostgreSQL receipt state and native birth disagree")
        if (self.pidfile is not None or self.header is not None) != (self.state == "ready"):
            raise ValueError("PostgreSQL ready receipt requires its bound pidfile")
        if self.state == "ready" and (self.pidfile is None or self.header is None):
            raise ValueError("PostgreSQL pidfile binding is incomplete")
        if self.platform == "linux" and self.owner is not None and self.owner.starttime is None:
            raise ValueError("PostgreSQL Linux custody requires native start ticks")
        return self

    def process(self) -> OwnedProcess:
        if self.owner is None:
            raise RuntimeError("PostgreSQL launch has no captured native birth; reconcile custody")
        return OwnedProcess(self.owner.pid, self.owner.create_time, self.owner.starttime)


def _data_path(data: Path) -> Path:
    canonical = data.parent.resolve() / data.name
    if canonical.is_symlink():
        raise RuntimeError("PostgreSQL data directory cannot be a symlink into another home")
    return canonical


def receipt_path(data: Path) -> Path:
    return _data_path(data).parent / "run" / "postgres.json"


def _inode(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino


def boot_id() -> str:
    Platform.current()
    boot = native_boot_id()
    if boot is None:
        raise RuntimeError("cannot observe PostgreSQL native boot scope")
    return boot


_boot = boot_id


def write(data: Path, receipt: Receipt) -> None:
    path = receipt_path(data)
    ensure_private_dir(path.parent)
    write_text_atomic(path, receipt.model_dump_json() + "\n", sync_parent=True)


def read_receipt(data: Path) -> Receipt | None:
    path = receipt_path(data)
    try:
        raw = regular_bytes(path, max_bytes=16 * 1024)
    except FileNotFoundError:
        return None
    receipt = Receipt.model_validate_json(raw)
    if receipt.data != str(data) or receipt.platform != Platform.current().system:
        raise RuntimeError("PostgreSQL custody names another data directory or platform")
    return receipt


_read = read_receipt


# meiva:note
# What `require_closed` asks of one process: `psutil.Process` is one, a scripted process that
# exits or refuses to be read mid-look is the other.
class ProcessUids(Protocol):
    @property
    def real(self) -> int: ...


class ProcessView(Protocol):
    @property
    def pid(self) -> int: ...

    def uids(self) -> ProcessUids: ...

    def status(self) -> str: ...

    def name(self) -> str: ...

    def cwd(self) -> str: ...

    def cmdline(self) -> list[str]: ...

    def is_running(self) -> bool: ...


# meiva:note
# The processes this user can see right now, for `require_closed` to judge.
def live_processes() -> Iterable[ProcessView]:
    return psutil.process_iter(["pid"])


def _directory_matches(process: ProcessView, data: Path, receipt: Receipt | None) -> bool:
    cwd = Path(process.cwd())
    if cwd.resolve() == data:
        return True
    if receipt is not None and _inode(cwd) == receipt.directory:
        return True
    argv = process.cmdline()
    return "-D" in argv and Path(argv[argv.index("-D") + 1]).resolve() == data


# meiva:note
# Whether a process that refused a read is simply gone: macOS answers the argv read of a process
# that is exiting with "access denied", so the refusal alone says nothing about custody. A
# process that is still running and unreadable is not excused.
def _vanished(process: ProcessView) -> bool:
    try:
        return not process.is_running() or process.status() in {
            psutil.STATUS_ZOMBIE,
            psutil.STATUS_DEAD,
        }
    except psutil.NoSuchProcess:
        return True


def require_closed(
    data: Path, receipt: Receipt | None, *, processes: Iterable[ProcessView]
) -> None:
    """A dead postmaster alone cannot prove that its old workers have closed."""
    session = (
        receipt.owner.pid
        if receipt is not None and receipt.owner is not None and receipt.boot_id == boot_id()
        else None
    )
    for process in processes:
        try:
            if process.uids().real != os.getuid() or process.status() in {
                psutil.STATUS_ZOMBIE,
                psutil.STATUS_DEAD,
            }:
                continue
            pg_named = process.name() in {"postgres", "postmaster"}
            # Include archive commands and other children with non-PG names.
            # A prior boot never donates its recycled session ID as authority.
            if (session is not None and os.getsid(process.pid) == session) or (
                pg_named and _directory_matches(process, data, receipt)
            ):
                raise RuntimeError("PostgreSQL descendants or an unrecorded process retain custody")
        except (psutil.NoSuchProcess, ProcessLookupError):
            continue
        except psutil.AccessDenied:
            if _vanished(process):
                continue
            raise


def read_pidfile(data: Path, receipt: Receipt) -> tuple[tuple[int, int], tuple[str, ...]]:
    path = data / "postmaster.pid"
    before = _inode(path)
    lines = regular_bytes(path, max_bytes=8192).decode().splitlines()
    owner = receipt.process()
    if (
        len(lines) < 4
        or int(lines[0]) != owner.pid
        or Path(lines[1]).resolve() != data
        or int(lines[3]) != receipt.port
        or before != _inode(path)
    ):
        raise RuntimeError("PostgreSQL pidfile does not describe the captured native child")
    # meiva:note
    # The wall timestamp is bound as file data, never compared to native birth.
    header = tuple(lines[:4])
    if receipt.pidfile is not None and (receipt.pidfile, receipt.header) != (before, header):
        raise RuntimeError("PostgreSQL pidfile changed after native admission")
    return before, header


def verify_captured_process(
    data: Path,
    receipt: Receipt,
    *,
    directory_inode: tuple[int, int],
    process_name: str,
    argv: Sequence[str],
    cwd: Path,
    sid: int,
    captured_birth: OwnedProcess,
    live: bool,
) -> OwnedProcess:
    owner = receipt.process()
    if (
        directory_inode != receipt.directory
        or process_name not in {"postgres", "postmaster"}
        or "-D" not in argv
        or Path(argv[list(argv).index("-D") + 1]).resolve() != data
        or cwd.resolve() != data
        or sid != owner.pid
        or not owner.same_birth(captured_birth)
        or not live
    ):
        raise RuntimeError("cannot verify this home's captured PostgreSQL process")
    return owner


def _require_process(data: Path, receipt: Receipt) -> OwnedProcess:
    owner = receipt.process()
    process = psutil.Process(owner.pid)
    return verify_captured_process(
        data,
        receipt,
        directory_inode=_inode(data),
        process_name=process.name(),
        argv=process.cmdline(),
        cwd=Path(process.cwd()),
        sid=os.getsid(owner.pid),
        captured_birth=OwnedProcess.capture(process),
        live=owner.live(),
    )


def observe(data: Path) -> OwnedProcess | None:
    """Read exact launch evidence. Missing, interrupted or unreadable is not adoption."""
    data = _data_path(data)
    receipt = read_receipt(data)
    if receipt is None:
        if (data / "postmaster.pid").exists():
            raise RuntimeError("PostgreSQL pidfile has no native launch receipt; reconcile custody")
        require_closed(data, None, processes=live_processes())
        return None
    if receipt.state == "pending":
        raise RuntimeError("PostgreSQL launch custody is unresolved; reconcile pending receipt")
    if receipt.boot_id != boot_id() or receipt.owner is None or not receipt.process().live():
        require_closed(data, receipt, processes=live_processes())
        return None
    owner = _require_process(data, receipt)
    try:
        read_pidfile(data, receipt)
    except FileNotFoundError:
        if receipt.state != "captured":
            raise
    if not owner.live():
        raise RuntimeError("PostgreSQL exited during custody observation")
    return owner


@contextmanager
def admission() -> Generator[Callable[[], None]]:
    pending: list[int] = []
    previous: dict[int, Callable[[int, FrameType | None], Any] | int | None] = {}

    def capture(signum: int, _frame: FrameType | None) -> None:
        pending.append(signum)

    def check() -> None:
        if pending:
            raise KeyboardInterrupt(f"PostgreSQL admission interrupted by signal {pending[0]}")

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, capture)
        yield check
        check()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


_admission = admission


def record_spawned_process(
    data: Path,
    receipt: Receipt,
    child: subprocess.Popen[bytes],
    *,
    retained_children: list[subprocess.Popen[bytes]],
    captured_owner: OwnedProcess | None = None,
) -> Receipt:
    retained_children.append(child)
    owner = (
        captured_owner
        if captured_owner is not None
        else OwnedProcess.capture(psutil.Process(child.pid))
    )
    owner.birth_key()
    captured = receipt.model_copy(
        update={
            "state": "captured",
            "owner": ExpectedProcess(
                pid=owner.pid, create_time=owner.birth, starttime=owner.starttime
            ),
        }
    )
    write(data, captured)
    return captured


def spawn_postgres(
    data: Path,
    receipt: Receipt,
    argv: list[str],
    env: dict[str, str],
    *,
    retained_children: list[subprocess.Popen[bytes]],
    captured_owner: OwnedProcess | None = None,
) -> Receipt:
    write(data, receipt)
    with _admission(), (data / "pg.log").open("ab") as log:
        try:
            child = subprocess.Popen(  # noqa: S603 — explicit resolved PostgreSQL argv, never a shell
                argv,
                cwd=data,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        except OSError:
            # meiva:note
            # Popen's normal exec error guarantees it reaped its child. A
            # BaseException or later error has no such guarantee: keep pending.
            write(data, receipt.model_copy(update={"state": "not-started"}))
            raise
        return record_spawned_process(
            data,
            receipt,
            child,
            retained_children=retained_children,
            captured_owner=captured_owner,
        )


def _complete_start(
    data: Path,
    receipt: Receipt,
    ready: Callable[[], bool],
    timeout: float,
    timers: Timers,
) -> OwnedProcess:
    """One readiness and durable admission boundary for fresh and retained births."""
    owner = receipt.process()
    deadline = timers.monotonic() + timeout
    while owner.live():
        protocol_ready = ready()
        if timers.monotonic() >= deadline:
            raise RuntimeError("PostgreSQL did not become ready; captured custody is retained")
        if protocol_ready:
            _require_process(data, receipt)
            inode, header = read_pidfile(data, receipt)
            require_listener(owner, receipt.port)
            admitted = receipt.model_copy(
                update={"state": "ready", "pidfile": inode, "header": header}
            )
            if admitted != receipt:
                write(data, admitted)
            return owner
        timers.sleep(0.05)
    require_closed(data, receipt, processes=live_processes())
    raise RuntimeError(f"PostgreSQL exited before readiness; inspect {data / 'pg.log'}")


def verify_retained_owner(
    port: int,
    expected: OwnedProcess,
    observed: OwnedProcess | None,
    receipt: Receipt | None,
) -> Receipt:
    if observed is None or not observed.same_birth(expected):
        raise RuntimeError("PostgreSQL identity changed before retained startup")
    if receipt is None or receipt.port != port:
        raise RuntimeError("PostgreSQL retained receipt does not match the startup port")
    return receipt


def _retained_start(data: Path, port: int, expected: OwnedProcess) -> Receipt:
    owner = observe(data)
    receipt = verify_retained_owner(port, expected, owner, read_receipt(data))
    if receipt.state == "ready" and (owner is None or not owner.send_signal(signal.SIGHUP)):
        raise RuntimeError("PostgreSQL exited before retained startup")
    return receipt


def start(
    data: Path,
    port: int,
    argv: list[str],
    env: dict[str, str],
    *,
    ready: Callable[[], bool],
    timeout: float = 60,
    expected: OwnedProcess | None = None,
    retained_children: list[subprocess.Popen[bytes]],
    timers: Timers,
) -> OwnedProcess:
    """Complete readiness for a new child or the exact retained postmaster.

    The protocol callback must itself be bounded. A retained birth is never
    replaced, and captured admission becomes ready only after the same checks
    as a fresh launch. Every path runs under the home PG control lock.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("PostgreSQL startup timeout must be finite and positive")
    data = _data_path(data)
    data.stat()
    path = receipt_path(data)
    ensure_private_dir(path.parent)
    with file_lock(path.with_suffix(".lock"), timeout_s=timeout):
        if expected is not None:
            retained = _retained_start(data, port, expected)
            owner = _complete_start(data, retained, ready, timeout, timers)
            if retained.state == "captured" and not owner.send_signal(signal.SIGHUP):
                raise RuntimeError("PostgreSQL exited before retained startup")
            return owner
        if observe(data) is not None:
            raise RuntimeError("PostgreSQL is already running; startup cannot replace its birth")
        require_listener(None, port, required=False)
        captured = spawn_postgres(
            data,
            Receipt(
                data=str(data),
                directory=_inode(data),
                port=port,
                platform=Platform.current().system,
                boot_id=boot_id(),
                state="pending",
            ),
            argv,
            env,
            retained_children=retained_children,
        )
        return _complete_start(data, captured, ready, timeout, timers)


@dataclass(frozen=True)
class Escalation:
    """A fast shutdown that did not finish and was ended by an immediate one."""

    detail: str
    killed: tuple[int, ...]  # pids SIGKILLed after the immediate shutdown (verified descendants)


def _wait_all_gone(members: set[OwnedProcess], wait: float, timers: Timers) -> bool:
    deadline = timers.monotonic() + wait
    while any(member.live() for member in members):
        if timers.monotonic() >= deadline:
            return False
        timers.sleep(0.05)
    return True


def _escalate(
    owner: OwnedProcess,
    tree: set[OwnedProcess],
    fast_wait: float,
    immediate_wait: float,
    kill_wait: float,
    timers: Timers,
) -> Escalation:
    """End a fast shutdown that is stuck: SIGQUIT, then SIGKILL what outlives it.

    Postgres stays stuck in a fast shutdown while its archiver waits on an archive command
    that never returns. SIGQUIT is Postgres' immediate shutdown (no checkpoint; the next
    start replays WAL, and segments not yet archived stay in `pg_wal` for the archiver).
    The archiver dies with it but its archive command, a child of the archiver, is
    orphaned, so the tree captured before the signal is the only proof of whose it is:
    every survivor still matching its recorded birth is SIGKILLed, children before the
    postmaster, and nothing is ever signalled by name or pid alone.
    """
    detail = (
        f"fast shutdown did not complete within {fast_wait:.0f}s "
        "(a blocked archive command is the usual cause)"
    )
    logger.error(f"[postgres] {detail}; escalating to immediate shutdown")
    for member in tuple(tree):  # a command the archiver started after the first capture
        retain_processes(tree, capture_tree(member))
    owner.send_signal(signal.SIGQUIT)
    _wait_all_gone({owner}, immediate_wait, timers)
    survivors = sorted(
        (member for member in tree if member.live()), key=lambda member: member == owner
    )
    for member in survivors:
        member.send_signal(signal.SIGKILL)
    if not _wait_all_gone(tree, kill_wait, timers):
        alive = sorted(member.pid for member in tree if member.live())
        logger.error(f"[postgres] processes outlived SIGKILL: {alive}")
        raise RuntimeError(
            f"PostgreSQL immediate shutdown left processes {alive} alive; custody retained"
        )
    killed = tuple(member.pid for member in survivors)
    logger.error(f"[postgres] immediate shutdown done; killed leftover processes {list(killed)}")
    return Escalation(detail, killed)


def reap_retained_child(owner_pid: int, retained_children: list[subprocess.Popen[bytes]]) -> None:
    for child in tuple(retained_children):
        if child.pid == owner_pid:
            child.wait(timeout=1)
            retained_children.remove(child)


def stop(
    data: Path,
    *,
    timers: Timers,
    expected: OwnedProcess | None = None,
    timeout: float = 60,
    immediate_wait: float | None = None,
    kill_wait: float | None = None,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> Escalation | None:
    """Fast clean shutdown, followed by exact native tree and endpoint closure.

    A fast shutdown that has not finished after `timeout` fails with custody retained,
    unless `immediate_wait` and `kill_wait` are given: then it is ended by an immediate
    shutdown (waiting at most `immediate_wait` for the postmaster) and a SIGKILL of the
    recorded descendants that outlive it (waiting at most `kill_wait`), and the returned
    `Escalation` says so. Returns None for a shutdown that finished by itself.
    """
    data = _data_path(data)
    path = receipt_path(data)
    ensure_private_dir(path.parent)
    with file_lock(path.with_suffix(".lock"), timeout_s=timeout):
        owner = observe(data)
        receipt = read_receipt(data)
        if owner is None:
            if receipt is not None:
                require_listener(None, receipt.port, required=False)
            return None
        if expected is not None and not owner.same_birth(expected):
            raise RuntimeError("PostgreSQL replacement cannot inherit retained stop authority")
        if receipt is None:
            raise RuntimeError("PostgreSQL receipt disappeared before native control")
        require_listener(owner, receipt.port, required=False)
        tree = capture_tree(owner)
        owner.send_signal(signal.SIGINT)  # PostgreSQL fast, checkpointed shutdown.
        escalation: Escalation | None = None
        if not _wait_all_gone(tree, timeout, timers):
            if immediate_wait is None or kill_wait is None:
                raise RuntimeError("PostgreSQL native shutdown did not complete; custody retained")
            escalation = _escalate(owner, tree, timeout, immediate_wait, kill_wait, timers)
        require_closed(data, receipt, processes=live_processes())
        require_listener(None, receipt.port, required=False)
        if retained_children is not None:
            reap_retained_child(owner.pid, retained_children)
        return escalation
