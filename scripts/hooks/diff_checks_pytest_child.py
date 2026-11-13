# meiva:note
# Signal handling scoped to the one pytest process this invocation starts.

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import FrameType
from typing import NoReturn


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


def stop_and_confirm(child: subprocess.Popen[str] | None) -> None:
    if child is None:
        return
    try:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                _ = child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                _ = child.communicate(timeout=5)
        require_group_absent(child.pid)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PytestChildUnreapedError("pytest child exit unconfirmed; scratch retained") from error


def raise_interruption(signum: int) -> NoReturn:
    raise PytestInterrupted(signum)


def finish_cleanup(
    cleanup: PytestChildUnreapedError | None,
    primary: PytestInterrupted | KeyboardInterrupt | None,
    pending: int | None,
) -> None:
    if primary is None and pending is not None:
        primary = PytestInterrupted(pending)
    if cleanup is not None:
        if primary is None:
            raise cleanup
        diagnostic = f"pytest cleanup incomplete during interruption: {type(cleanup).__name__}"
        print(diagnostic, file=sys.stderr)
        primary.add_note(diagnostic)
        raise primary from cleanup
    if primary is not None:
        raise primary


def run_pytest_child(
    argv: Sequence[str],
    root: Path,
    *,
    environment: Mapping[str, str],
    start: Callable[[Sequence[str], Path, Mapping[str, str]], subprocess.Popen[str]] | None = None,
    stop: Callable[[subprocess.Popen[str] | None], None] | None = None,
) -> tuple[int, str, float]:
    started = time.monotonic()
    child: subprocess.Popen[str] | None = None
    interrupted: list[int] = []
    primary_interrupt: PytestInterrupted | KeyboardInterrupt | None = None
    cleaning = False

    def interrupt(signum: int, _frame: FrameType | None) -> None:
        interrupted.append(signum)
        if child is not None and not cleaning:
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
            raise_interruption(interrupted[-1])
        output, _ = child.communicate()
        return child.returncode, output, time.monotonic() - started
    except (PytestInterrupted, KeyboardInterrupt) as error:
        primary_interrupt = error
        raise
    finally:
        # meiva:note
        # Cleanup keeps recording TERM without interrupting its bounded wait. Restore
        # the caller's handler before resolving the recorded interruption and failure.
        cleaning = True
        cleanup_error: PytestChildUnreapedError | None = None
        try:
            (stop or stop_and_confirm)(child)
        except PytestChildUnreapedError as error:
            cleanup_error = error
        except KeyboardInterrupt as error:
            if primary_interrupt is None:
                primary_interrupt = error
            cleanup_error = PytestChildUnreapedError("pytest cleanup interrupted; scratch retained")
        finally:
            _ = signal.signal(signal.SIGTERM, previous)
        finish_cleanup(cleanup_error, primary_interrupt, interrupted[-1] if interrupted else None)
