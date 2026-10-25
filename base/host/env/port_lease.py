# meiva:note
# Cross-process leases on a localhost port band no kernel hands out.
#
# A test that needs a port number — a home's recorded table, a fixture's "one free
# port for this redis" — cannot bind it now and keep it for the server that binds it
# a moment later, so the number has to be *reserved* instead. This module is that
# reservation: while a lease holder lives, no other holder on this host is handed
# the same number.
#
# Three facts make the promise hold without a daemon:
#
# - The band (`BAND_FIRST`..`BAND_LAST`) sits above every `FIXED_PORTS` entry and
#   below every platform's ephemeral range, so `bind(("127.0.0.1", 0))` — the idiom
#   the rest of the repository uses, `base.cluster.dataplane.pg_tools.free_port`
#   included — can never be handed a leased number. The two populations are
#   disjoint, so the throwaway data-plane allocator needs no knowledge of this
#   module, and neither does a test helper this module did not replace.
# - Allocation — probe, registry read, publish — happens under one host-wide
#   `flock`, so two workers cannot pick the same number out of the same gap.
# - Liveness is a `flock` the holder keeps open for its whole life, the oracle
#   `base.cluster.dataplane.pg_tools` uses for throwaway instances: the kernel drops
#   it however the holder dies, so a dead holder's numbers are reusable with no pid
#   bookkeeping — and no pid recycled onto an unrelated process can keep an
#   abandoned lease alive.
#
# There is no in-process state: this process's lease is one file named by its pid,
# and the file is the truth. `lease_ports` opens that file on every call.
#
# The boundary of the promise, stated once so nobody expects more:
#
# - **One OS user.** The registry directory carries the uid (a user's own directory,
#   the same choice `base.cluster.dataplane.pg_tools` makes for throwaway data): two
#   users on one host have two registries, and could not stop each other's servers
#   anyway.
# - **One container.** `/dev/shm` is per container, so two containers have two
#   registries. That matches the boundary that matters — a container has its own
#   network namespace and therefore its own port space — but it means a lease is
#   never a cross-container reservation.
#
# What a lease is not: a bind. Between the lease and the server's own bind, a
# process outside the suite can take the number, and the server then fails to bind
# exactly as it would have before. The lease removes collisions *among the holders*,
# which is the class a test session can control.
#
# No product path calls this module. It lives in `base` because `base` is the lowest
# layer every test tree (`tests/`, `cli/*/tests`, `gateway/tests`, `base/*/tests`)
# may import, and `base.host.env` already owns the host's port vocabulary
# (`port_table`).

from __future__ import annotations

import json
import os
import secrets
import socket
import tempfile
from pathlib import Path
from typing import TypeGuard


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


# meiva:note
# The band. 21000 is the documented start of the private range (`port_table` and
# `base/cluster/tests/test_fixed_ports.py::_PRIVATE_TEST_PORTS_START`), above every
# `FIXED_PORTS` entry (max 19531). 32767 is one below the lowest ephemeral range of
# any supported platform: Linux starts at 32768 by default
# (`net.ipv4.ip_local_port_range`) and macOS at 49152.
BAND_FIRST = 21_000
BAND_LAST = 32_767


# meiva:note
# `count` distinct ports no live holder on this host has leased.
def lease_ports(count: int) -> list[int]:
    if count < 1:
        raise ValueError(f"lease_ports needs a positive count, not {count}")
    root = _root()
    path = root / f"lease-{os.getpid()}.json"
    with _host_lock(root):
        _hold(path)
        picked = _pick(count, _live_ports(root))
        _publish(path, _ports_of(path) + picked)
    return picked


# meiva:note
# The numbers this process holds, oldest first (diagnostics and tests).
def leased_ports() -> list[int]:
    return _ports_of(_root() / f"lease-{os.getpid()}.json")


# meiva:note
# The directory the registry lives in. Exposed so a reader (a test, an operator with a
# stale port) sees the lease files instead of inferring them.
def registry_dir() -> Path:
    return _root()


# meiva:note
# `holder pid -> the ports it holds`, for every live holder here, ours included.
# The same read the allocator makes, exposed because a lease that outlived its
# holder is the one failure this design cannot make loud: a test asserts the
# registry, not the absence of a symptom.
def live_holders() -> dict[int, list[int]]:
    root = _root()
    with _host_lock(root):
        return _holders(root)


# meiva:note
# The host-wide meeting point: one directory per user, shared by every process —
# never per home, because the port space belongs to the kernel, not to a cluster.
# tmpfs when the platform has one (`/dev/shm`, the fixed host-wide path), else the
# OS temp dir — the same choice the throwaway data plane makes for its instances, and
# for the same reason: this is host coordination, not durable state.
def _root() -> Path:
    shm = Path("/dev/shm")  # noqa: S108
    base = shm if shm.is_dir() else Path(tempfile.gettempdir())
    return ensure_registry(base / f"meiva-port-lease-{os.getuid()}")


# meiva:note
# `root`, created and proven writable, or the run stops.
#
# There is deliberately no fallback to `bind(':0')`. A silent fallback would restore
# exactly the state this module exists to end — an unattributable number — and it
# would do it only when the machine is already unhealthy, where nobody is watching.
# The message names the directory and the reason, and says what to do; it offers no
# switch that could turn the guarantee off for good.
def ensure_registry(root: Path) -> Path:
    try:
        root.mkdir(mode=0o700, exist_ok=True)
        probe = root / f".probe-{secrets.token_hex(4)}"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"the port lease registry at {root} is unusable ({exc.strerror or exc}). Every "
            "test port number comes from it and there is no fallback to an unattributable "
            "number: repair that directory (or delete it so it is recreated) and run again."
        ) from exc
    return root


class _HostLock:
    # meiva:note
    # The one lock every allocator on this host serializes on.
    def __init__(self, root: Path) -> None:
        self._path = root / "alloc.lock"
        self._fd = -1

    def __enter__(self) -> None:
        import fcntl

        self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self._fd, fcntl.LOCK_EX)

    def __exit__(self, *_exc: object) -> None:
        import fcntl

        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = -1


def _host_lock(root: Path) -> _HostLock:
    return _HostLock(root)


# meiva:note
# Take this process's lifetime flock on its lease file, once.
#
# The descriptor is deliberately never stored and never closed: an open descriptor
# with no Python reference stays open until the process exits, and the kernel drops
# its flock then, which is the liveness oracle the whole design rests on. A second
# call finds the lock already held by this process (a live pid is one process) and
# closes its extra descriptor. Acquiring it instead means the file was left by a
# dead holder whose pid we now own, so its body is stale and starts empty.
def _hold(path: Path) -> None:
    import fcntl

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return
    os.ftruncate(fd, 0)


# meiva:note
# Every live holder's numbers. Caller holds the host lock.
#
# A lease file whose flock can be taken belongs to a dead process — the kernel
# released its lock — so the file is dropped and its numbers are free again. An entry
# that vanishes between the glob and the open is a racing allocator's, not ours.
def _holders(root: Path) -> dict[int, list[int]]:
    import fcntl

    own = f"lease-{os.getpid()}.json"
    holders: dict[int, list[int]] = {}
    for path in sorted(root.glob("lease-*.json")):
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            continue
        try:
            if path.name == own:
                holders[os.getpid()] = _ports_from(fd)
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                pid = _pid_from(fd)
                holders[pid] = _ports_from(fd)
            else:
                path.unlink(missing_ok=True)
        finally:
            os.close(fd)
    return holders


def _live_ports(root: Path) -> set[int]:
    ports: set[int] = set()
    for held in _holders(root).values():
        ports.update(held)
    return ports


def _ports_of(path: Path) -> list[int]:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return []
    try:
        return _ports_from(fd)
    finally:
        os.close(fd)


def _ports_from(fd: int) -> list[int]:
    ports = _body(fd).get("ports")
    if not _is_object_list(ports):
        return []
    return [port for port in ports if isinstance(port, int)]


def _pid_from(fd: int) -> int:
    pid = _body(fd).get("pid")
    return pid if isinstance(pid, int) else -1


# meiva:note
# A lease file's JSON body, `{}` when it is unreadable.
#
# A run killed between creating its lease and writing it leaves a truncated body;
# that is a crash artifact, not a caller mistake, so it degrades to "holds nothing"
# rather than aborting every other holder's allocation.
def _body(fd: int) -> dict[str, object]:
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        body: object = json.loads(os.read(fd, 1 << 20))
    except (ValueError, UnicodeDecodeError):
        return {}
    return body if _is_object_dict(body) else {}


# meiva:note
# Write this holder's numbers into its lease file. Caller holds the host lock.
def _publish(path: Path, ports: list[int]) -> None:
    body = json.dumps({"pid": os.getpid(), "ports": ports}).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.write(fd, body)
        os.ftruncate(fd, len(body))
    finally:
        os.close(fd)


# meiva:note
# `count` numbers from the band that no holder has and this host can bind now.
#
# One random start, then a forward scan with wrap: a rotating allocator that spreads
# concurrent runs across the band instead of marching them into the same low numbers.
def _pick(count: int, held: set[int]) -> list[int]:
    span = BAND_LAST - BAND_FIRST + 1
    if len(held) + count > span:
        raise RuntimeError(
            f"the lease band {BAND_FIRST}-{BAND_LAST} has {span} ports; "
            f"{len(held)} held and {count} more requested"
        )
    start = BAND_FIRST + secrets.randbelow(span)
    picked: list[int] = []
    for offset in range(span):
        if len(picked) == count:
            break
        port = BAND_FIRST + (start - BAND_FIRST + offset) % span
        if port in held or not _bindable(port):
            continue
        picked.append(port)
        held.add(port)
    if len(picked) < count:
        raise RuntimeError(
            f"only {len(picked)} of {count} ports in {BAND_FIRST}-{BAND_LAST} are free to lease"
        )
    return picked


# meiva:note
# Whether this host can bind `port` right now (a port in TIME_WAIT cannot).
def _bindable(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True
