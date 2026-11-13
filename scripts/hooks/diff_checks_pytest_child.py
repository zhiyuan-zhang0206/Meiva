# meiva:note
# Signal handling scoped to the one pytest process this invocation starts.

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
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


def require_group_absent(group: int) -> None:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return
    except OSError as error:
        raise PytestChildUnreapedError(
            "pytest group absence unconfirmed; scratch retained"
        ) from error
    raise PytestChildUnreapedError("pytest leader exited but group remains; scratch retained")


def start_pytest_child(
    argv: Sequence[str], root: Path, environment: Mapping[str, str]
) -> subprocess.Popen[str]:
    # meiva:note
    # The supplied argv is the scoped pytest selection, run without a shell.
    return subprocess.Popen(  # noqa: S603
        list(argv),
        cwd=root,
        env=dict(environment),
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def run_pytest_child(
    argv: Sequence[str],
    root: Path,
    *,
    environment: Mapping[str, str],
    start: Callable[[Sequence[str], Path, Mapping[str, str]], subprocess.Popen[str]] | None = None,
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
        try:
            child = (start or start_pytest_child)(argv, root, environment)
        except BaseException as error:
            if interrupted:
                raise PytestInterrupted(interrupted[-1]) from error
            raise
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
            if child is not None:
                require_group_absent(child.pid)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PytestChildUnreapedError(
                "pytest child exit unconfirmed; scratch retained"
            ) from error
        finally:
            _ = signal.signal(signal.SIGTERM, previous)
