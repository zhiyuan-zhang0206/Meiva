#!/usr/bin/env python
# meiva:note
# apt-get and dpkg hold an fcntl write lock on /var/lib/dpkg/lock-frontend for their whole
# run. A bounded install retry that starts attempt 2 while attempt 1's apt is still alive
# therefore fails on the lock, and the caller reads that as another network outage: on
# 2026-10-22 one unreachable archive became "the cached archives cannot satisfy the system
# deps" and failed a Playwright job whose archive cache was complete. Probing the same lock
# apt does (never a guess from process names) and waiting for it, with a ceiling, turns the
# collision into a wait; when the ceiling expires the caller learns who holds the lock, so
# the failure it reports is the failure it has.
#
# Exit 0 = free (now or after waiting); 1 = still held when the ceiling expired; 2 = the
# lock could not be inspected — apt's lock file is root-owned, so run this under sudo.
# meiva:link scripts/provision/docs/apt-lock.meiva.okf.md

from __future__ import annotations

import argparse
import errno
import fcntl
import os
import sys
import time
from pathlib import Path

DEFAULT_LOCK = Path("/var/lib/dpkg/lock-frontend")
EXIT_FREE = 0
EXIT_HELD = 1
EXIT_UNINSPECTABLE = 2


# meiva:note
# A missing lock file means nothing holds it — the caller is about to run apt, which
# creates the file itself with the ownership the platform expects. Creating it here could
# leave a lock file apt cannot use.
# meiva:link scripts/provision/docs/apt-lock.meiva.okf.md
def lock_is_free(path: Path) -> bool:
    if not path.exists():
        return True
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError as exc:
        if exc.errno == errno.EACCES:
            raise PermissionError(f"{path} is not writable by this user; run under sudo") from exc
        raise
    try:
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        fcntl.lockf(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


# meiva:note
# Only used to explain an expired wait: the verdict itself comes from the lock probe, which
# is the same mechanism apt uses. /proc answers on Linux only, so an empty result means
# "could not say", never "nobody".
# meiva:link scripts/provision/docs/apt-lock.meiva.okf.md
def holders(path: Path) -> list[int]:
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    target = str(path)
    found: list[int] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            descriptors = list((entry / "fd").iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                if str(descriptor.readlink()) == target:
                    found.append(int(entry.name))
                    break
            except OSError:
                continue
    return sorted(found)


def wait_for_lock(path: Path, timeout: float, interval: float) -> tuple[bool, float]:
    started = time.monotonic()
    while True:
        if lock_is_free(path):
            return True, time.monotonic() - started
        if time.monotonic() - started >= timeout:
            return False, time.monotonic() - started
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wait for the dpkg frontend lock, bounded.")
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args(argv)

    try:
        free, waited = wait_for_lock(args.lock, args.timeout, args.interval)
    except PermissionError as exc:
        print(f"apt lock: {exc}", file=sys.stderr)
        return EXIT_UNINSPECTABLE
    if free:
        if waited >= args.interval:
            print(f"apt lock: free after waiting {waited:.1f}s")
        return EXIT_FREE
    held_by = holders(args.lock)
    who = (
        ", ".join(str(pid) for pid in held_by)
        if held_by
        else "unknown (no /proc entry could read it)"
    )
    print(
        f"apt lock: {args.lock} is still held after {waited:.1f}s by pid(s) {who} — "
        f"that is a held lock, not a package or network failure",
        file=sys.stderr,
    )
    return EXIT_HELD


if __name__ == "__main__":
    raise SystemExit(main())
