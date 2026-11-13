# meiva:note
# Signal handling scoped to the one pytest process this invocation starts.

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import FrameType


class PytestInterrupted(BaseException):
    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(signum)


class PytestChildUnreapedError(RuntimeError):
    # meiva:note
    # A child whose exit was not confirmed still owns its scratch.
    pass


def run_pytest_child(
    argv: Sequence[str], root: Path, *, environment: Mapping[str, str]
) -> tuple[int, str, float]:
    started = time.monotonic()
    child: subprocess.Popen[str] | None = None
    interrupted: list[int] = []

    def interrupt(signum: int, _frame: FrameType | None) -> None:
        interrupted.append(signum)
        if child is not None:
            raise PytestInterrupted(signum)

    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        # meiva:note
        # explicit selected pytest argv, no shell
        child = subprocess.Popen(  # noqa: S603
            list(argv),
            cwd=root,
            env=dict(environment),
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if interrupted:
            raise PytestInterrupted(interrupted[-1])
        output, _ = child.communicate()
        return child.returncode, output, time.monotonic() - started
    finally:
        # meiva:note
        # Restore the caller's signal contract only after our child has exited.
        # A second TERM during this bounded cleanup must not interrupt its wait.
        _ = signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            if child is not None and child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    _ = child.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    _ = child.communicate(timeout=5)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PytestChildUnreapedError(
                "pytest child exit unconfirmed; scratch retained"
            ) from error
        finally:
            _ = signal.signal(signal.SIGTERM, previous)
