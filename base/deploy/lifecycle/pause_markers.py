# meiva:note
# `meiva stop` used to take a maintenance hold in `run/deploy-pause-owner.json` and leave it on
# disk; the hold was deleted on 2026-10-20 and nothing reads or writes the file. A home a later
# release stopped and then started keeps a record saying `paused` for a machine that is serving,
# and anything that gates on that record — a monitor outside the fleet, a person reading `run/` —
# then reads a stop that never ends: this repository's wedge watch skipped its own checks for half
# an hour after the 2026-10-21 wave. A start is where the claim stops being true, so a start that
# reaches readiness removes the markers whose claim its own live state contradicts, and says which.
# meiva:link base/deploy/lifecycle/docs/pause-markers.meiva.okf.md

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from base.paths import meiva_home, run_dir

MARKER_GLOB = "*pause*"


def marker_paths(home: Path) -> list[Path]:
    return sorted(p for p in run_dir(home).glob(MARKER_GLOB) if p.is_file() or p.is_symlink())


# meiva:note
# An unreadable pid file is not a running process: the entry is written atomically by a process
# that has just started, so a missing or partial one means there is nothing to find. A pid this
# user may not signal counts as running — the conservative reading, so a live home is never
# called dead.
def agent_host_running(home: Path) -> bool:
    try:
        pid = int((run_dir(home) / "agent_host.pid").read_text().strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# meiva:note
# meiva-root holds the home's root lock exclusively for its whole life (the fleet probe reads it
# the same way), so a shared lock that succeeds proves no root runs. None means the lock could not
# be read at all, which is not the same as "not running".
def root_running(home: Path) -> bool | None:
    lock = run_dir(home) / "meiva-root" / "meiva-root.lock"
    try:
        handle = lock.open("rb")
    except FileNotFoundError:
        return False
    except OSError:
        return None
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


def claimed_state(path: Path) -> str:
    try:
        recorded = path.read_text()
    except OSError:
        return "state=unreadable"
    for candidate in ('"state": "', '"state":"'):
        start = recorded.find(candidate)
        if start >= 0:
            rest = recorded[start + len(candidate) :]
            end = rest.find('"')
            if end > 0:
                return f"state={rest[:end]}"
    return "state=unrecorded"


# meiva:note
# The live fact decides, never the file's age and never the command that wrote it: a marker goes
# only where the home serves (agent host and meiva-root both running), and a home that is not
# serving keeps every marker, because nothing there proves its claim wrong.
def sweep(home: Path | None = None) -> list[str]:
    target = meiva_home() if home is None else home
    markers = marker_paths(target)
    if not markers:
        return []
    serving = agent_host_running(target)
    root = root_running(target)
    if not serving or root is not True:
        named = ", ".join(path.name for path in markers)
        return [
            f"kept pause marker {named}: this home is not serving "
            f"(agent-host={'up' if serving else 'down'}, "
            f"meiva-root={'up' if root else 'unknown' if root is None else 'down'})"
        ]
    removed: list[str] = []
    for path in markers:
        state = claimed_state(path)
        try:
            path.unlink()
        except OSError as exc:
            removed.append(f"could not remove pause marker {path.name} ({state}): {exc}")
            continue
        removed.append(f"removed pause marker {path.name} ({state}) — this home is serving")
    return removed
