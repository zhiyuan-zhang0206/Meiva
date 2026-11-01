# meiva:note
# Single-instance ownership of one run directory (I1 invariant).
#
# A root supervisor owns exactly one tree per (machine x home); the instance
# lock makes a second launch refuse instead of racing the first. The agent host
# takes the same kind of lock under its own name. The lock is a
# `flock` on `<run_dir>/<name>` (`meiva-root.lock` unless the caller names another):
# the kernel ties it to the owning file
# description, so it is released automatically when the owning process dies —
# no stale-lock reclaim loop and no pid-identity heuristics.
#
# The lock fd is opened O_CLOEXEC, and units are spawned with `close_fds=True`
# elsewhere in this package, so a unit process can never inherit — and keep
# alive — the tree's lock.
#
# The lock uses `fcntl.flock`.

from __future__ import annotations

import logging
import os
from pathlib import Path

_LOCK_NAME = "meiva-root.lock"

_log = logging.getLogger(__name__)


# meiva:note
# Another live process already holds this instance lock.
class AlreadyRunningError(RuntimeError): ...


# meiva:note
# Take the instance lock `name` in `run_dir`; return the fd that owns it.
#
# The fd must stay open while this process owns the tree — closing it (or
# process exit) releases the lock. Raises AlreadyRunningError when another
# live owner holds it.
def acquire_instance_lock(run_dir: Path, name: str = _LOCK_NAME) -> int:
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / name
    import fcntl

    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise AlreadyRunningError(
            f"another process already holds the instance lock {lock_path}"
        ) from exc
    # meiva:note
    # The pid written here is a note for people reading the lock file; the fd's flock is what decides
    # ownership. It is read from this process on purpose: the six callers are themselves the process
    # that holds the tree (the launch driver, the CLI's hold probe, and the root, agent-host and
    # helper daemons), so a pid handed in would be the same value — and a wrong one would write a
    # holder into a file whose only job is to be read by a person debugging a stuck home.
    #
    # Repaying this entry means the lock starts being taken on another process's behalf; until then
    # the caller has nothing to contribute that this function does not already know.
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    _log.info("instance lock acquired at %s", lock_path)
    return fd


def release_instance_lock(fd: int) -> None:
    """Release the instance lock and close its fd (idempotent at process exit)."""
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError as exc:
        _log.debug("instance lock release failed: %s", exc)
    os.close(fd)
