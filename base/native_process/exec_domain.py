# meiva:note
# The process group of one exec child, signalled only while its leader is unreaped.

from __future__ import annotations

import signal
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, cast

from base.native_process.process_control import ProcessControl

KILL_GRACE_S = 2.0


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
        cls, argv: list[str], *, process_control: ProcessControl, **options: Any
    ) -> tuple[subprocess.Popen[bytes], ExecProcessDomain]:
        if {"process_group", "start_new_session", "text", "encoding", "universal_newlines"} & (
            options.keys()
        ):
            raise ValueError("exec launch owns process grouping and byte streams")
        proc = cast(
            "subprocess.Popen[bytes]",
            subprocess.Popen(  # noqa: S603
                argv, start_new_session=True, **options
            ),
        )
        return proc, cls(proc, process_control)

    # meiva:note
    # Signal the group while the leader is unreaped; a reaped leader means nothing to do.
    def signal(self, signum: int) -> None:
        # meiva:note
        # Popen.poll and wait take this lock to reap, so the leader cannot be
        # reaped between the check and the signal.
        with cast("Any", self.proc)._waitpid_lock:
            if self.proc.returncode is not None:
                return
            try:
                self.process_control.killpg(self.proc.pid, signum)
            except PermissionError:
                # meiva:note
                # XNU refuses to signal a group whose members are all zombies.
                if sys.platform != "darwin":
                    raise

    # meiva:note
    # SIGKILL the group if the leader is unreaped, then reap the leader.
    def kill_and_reap(self, timeout: float = KILL_GRACE_S) -> int:
        self.signal(signal.SIGKILL)
        return self.proc.wait(timeout=timeout)
