# meiva:note
# The process group of one exec child, signalled only while its leader is unreaped.

from __future__ import annotations

import signal
import subprocess
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeGuard

from base.native_process.os_platform import is_macos
from base.native_process.process_control import ProcessControl

KILL_GRACE_S = 2.0


class _LockedPopen(Protocol):
    _waitpid_lock: AbstractContextManager[object]


def _has_waitpid_lock(proc: object) -> TypeGuard[_LockedPopen]:
    return hasattr(proc, "_waitpid_lock")


# meiva:note
# A child that leads its own session and process group.
#
# The leader's zombie keeps the group number reserved until it is reaped, so
# every group signal sent before that reaches only this launch. After the reap
# the number may belong to an unrelated group: ``signal`` then does nothing.
# Callers must not ``poll``, ``wait`` or ``communicate`` through ``Popen``
# until they are done signalling.
@dataclass(frozen=True)
class ExecProcessDomain:
    proc: subprocess.Popen[bytes]
    process_control: ProcessControl

    @classmethod
    # meiva:note
    # The caller's argv is its own admitted command; this launch only adds the new session.
    def launch_posix(
        cls,
        argv: list[str],
        *,
        process_control: ProcessControl,
        cwd: str | Path | None = None,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        env: Mapping[str, str] | None = None,
        **options: object,
    ) -> tuple[subprocess.Popen[bytes], ExecProcessDomain]:
        if {"process_group", "start_new_session", "text", "encoding", "universal_newlines"} & (
            options.keys()
        ):
            raise ValueError("exec launch owns process grouping and byte streams")
        if options:
            raise TypeError(f"unexpected launch_posix option(s): {sorted(options)}")
        proc = subprocess.Popen(  # noqa: S603
            argv,
            cwd=cwd,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=env,
            start_new_session=True,
        )
        return proc, cls(proc, process_control)

    # meiva:note
    # Signal the group while the leader is unreaped; a reaped leader means nothing to do.
    def signal(self, signum: int) -> None:
        # meiva:note
        # Popen.poll and wait take this lock to reap, so the leader cannot be
        # reaped between the check and the signal.
        locked: object = self.proc
        if not _has_waitpid_lock(locked):
            raise RuntimeError("Popen lacks _waitpid_lock")
        with locked._waitpid_lock:
            if self.proc.returncode is not None:
                return
            try:
                self.process_control.killpg(self.proc.pid, signum)
            except PermissionError:
                # meiva:note
                # XNU refuses to signal a group whose members are all zombies.
                if not is_macos():
                    raise

    # meiva:note
    # SIGKILL the group if the leader is unreaped, then reap the leader.
    def kill_and_reap(self, timeout: float = KILL_GRACE_S) -> int:
        self.signal(signal.SIGKILL)
        return self.proc.wait(timeout=timeout)
