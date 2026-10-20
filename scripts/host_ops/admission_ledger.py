#!/usr/bin/env python3
# meiva:note
# Who else is running, and how much room they already claimed: the reservations a big job
# writes, the class lock it takes, and the staleness rules that let both heal without a
# daemon (pid gone or a different boot means the claim is void).
# meiva:link scripts/host_ops/docs/admission-run.meiva.okf.md
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

WAIT_POLL_SECONDS = 10.0


def current_boot_id() -> str:
    # meiva:note
    # A reservation survives only inside the boot that made it: same pid after a reboot is a
    # different process. Linux names the boot; macOS exposes its second.
    try:
        if sys.platform.startswith("linux"):
            return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        out = subprocess.run(
            ["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, check=False
        ).stdout
        return out.split("sec =")[1].split(",")[0].strip()
    except (OSError, IndexError):
        return "unknown"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def live_reservations(home: Path) -> list[dict[str, object]]:
    # meiva:note
    # Self-healing without a daemon: every start prunes the entries whose process is gone or
    # whose boot no longer matches, so a killed job needs no cleanup pass to stop counting.
    directory = home / "active"
    boot = current_boot_id()
    live: list[dict[str, object]] = []
    if not directory.is_dir():
        return live
    for path in directory.glob("*.json"):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pid = entry.get("pid")
        if entry.get("boot_id") != boot or not isinstance(pid, int) or not _pid_alive(pid):
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        live.append(entry)
    return live


def reserved_gib(entries: Sequence[dict[str, object]]) -> float:
    total = 0.0
    for entry in entries:
        peak = entry.get("peak_mb")
        jobs = entry.get("jobs")
        if isinstance(peak, (int, float)) and isinstance(jobs, int):
            total += float(peak) * jobs / 1024
    return total


def reserve(home: Path, entry: dict[str, object]) -> Path:
    directory = home / "active"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{entry['pid']}-{entry['boot_id']}.json"
    path.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def lock_path(home: Path, script: str) -> Path:
    directory = home / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{script}.lock"


def lock_holder(path: Path) -> dict[str, object] | None:
    # meiva:note
    # A lock file whose process died, or that outlived its boot, is stale: take it over
    # instead of waiting on a job nobody runs any more.
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pid = entry.get("pid")
    if entry.get("boot_id") != current_boot_id() or not isinstance(pid, int):
        return None
    return entry if _pid_alive(pid) else None


def take_lock(path: Path, entry: dict[str, object]) -> bool:
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, indent=2, sort_keys=True) + "\n")
    return True


def wait_for_lock(
    path: Path,
    entry: dict[str, object],
    *,
    max_wait: float,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, float]:
    # meiva:note
    # Waiting is not refusing: the job always starts. Whoever holds the lock is named, so a
    # waiting caller sees a reason instead of an apparently hung tool; the cap keeps a
    # forgotten lock from blocking the machine for a day.
    started = time.monotonic()
    while True:
        if take_lock(path, entry):
            return "acquired", time.monotonic() - started
        holder = lock_holder(path)
        if holder is None:
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        waited = time.monotonic() - started
        if waited >= max_wait:
            return "timeout", waited
        print(
            f"admission: waiting for {entry['script']} lock held by {holder.get('script')} "
            f"({holder.get('peak_mb')} MiB x {holder.get('jobs')}, since "
            f"{holder.get('started_at')}); {max_wait - waited:.0f}s left",
            file=sys.stderr,
        )
        sleep(min(WAIT_POLL_SECONDS, max_wait - waited))
