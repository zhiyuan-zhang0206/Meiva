# meiva:note
# One bounded host-local recovery pass; scheduled by the existing health check.

from __future__ import annotations

import fcntl
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from meiva.shell.completion_plan import Completion, read_completion


def _read_marker(marker: Path) -> Completion:
    # meiva:note
    # Receipts never choose an executable or a log outside their own directory.
    if marker.resolve() != marker.absolute() or marker.with_suffix(".log").is_symlink():
        raise ValueError("symlink completion receipt or log")
    if marker.stat().st_size > 65536:
        raise ValueError("completion receipt exceeds 64 KiB")
    return read_completion(marker.read_text(), marker)


@dataclass
class RecoveryResult:
    delivered: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    busy: bool = False


def recover(
    workspaces: Path,
    cli: Path,
    *,
    now: float,
    limit: int = 2,
    retry_after: float = 60,
    timeout: float = 10,
) -> RecoveryResult:
    # meiva:note
    # Keep failed/uncertain intents and their logs, clearing only CLI acceptance.
    #
    # A stable host lock prevents overlapping recovery passes. Receipt mtime is
    # the last attempt time, so a restarted health watcher respects retry spacing.
    # The trusted CLI path is an operator argument, never read from a receipt.
    if limit < 1 or retry_after < 0 or timeout <= 0:
        raise ValueError("invalid recovery budget")
    result = RecoveryResult()
    attempted = 0
    with (workspaces / ".completion-recovery.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result.busy = True
            return result
        markers: list[tuple[float, Path]] = []
        for marker in workspaces.glob("*/.shell_logs/*.undelivered"):
            try:
                markers.append((marker.lstat().st_mtime, marker))
            except FileNotFoundError:
                continue
        # meiva:note
        # Oldest attempts first: one permanently failing recipient must not
        # consume every future pass and starve the remaining intents.
        markers.sort()
        for _, marker in markers:
            name = str(marker)
            try:
                receipt = _read_marker(marker)
                if now - marker.stat().st_mtime < retry_after or attempted >= limit:
                    result.deferred.append(name)
                    continue
                # meiva:note
                # A timeout is an ambiguous acceptance: leave the intent and use
                # precisely the same client-message-id on the next health pass.
                os.utime(marker, (now, now))
                attempted += 1
                response = subprocess.run(  # noqa: S603
                    [str(cli), *receipt.argv()],
                    timeout=timeout,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if response.returncode != 0:
                    result.errors[name] = f"sender exit {response.returncode}"
                    continue
                marker.unlink(missing_ok=True)
                result.delivered.append(name)
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                result.errors[name] = str(exc)
    return result
