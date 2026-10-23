from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
from dataclasses import dataclass

from loguru import logger

from ._ruff_plan import FailureKind, RuffFailure, RuffRun, give_up_warning


@dataclass(frozen=True)
class RuffExit:
    returncode: int
    stdout: str


# meiva:note
# Absolute path to the `ruff` that ships in this interpreter's env, found beside `sys.executable`
# rather than through the ambient PATH: agent processes only have the venv's `bin` on PATH because
# their launcher prepends it, and a caller that skips that (a direct interpreter run, an eval
# harness) would silently lose every ruff stage. Falls back to a PATH lookup for an interpreter
# outside a ruff-carrying env.
def ruff_executable() -> str:
    return shutil.which("ruff", path=str(pathlib.Path(sys.executable).parent)) or "ruff"


# meiva:note
# The ruff this process runs, resolved once by the composition root and handed to every stage.
def system_ruff() -> RuffRun:
    return RuffRun(executable=ruff_executable())


def _failure_of(exc: BaseException) -> RuffFailure | None:
    if isinstance(exc, subprocess.TimeoutExpired):
        return RuffFailure(FailureKind.TIMEOUT, timeout=exc.timeout)
    if isinstance(exc, FileNotFoundError):
        return RuffFailure(FailureKind.MISSING)
    if isinstance(exc, UnicodeError):
        return RuffFailure(FailureKind.UNICODE, error_type=type(exc).__name__, error_text=str(exc))
    if isinstance(exc, OSError):
        return RuffFailure(FailureKind.OS_ERROR, errno=exc.errno, strerror=exc.strerror)
    return None


# meiva:note
# Run one ruff stage with the source on stdin. A stage that cannot run (ruff missing, over its
# budget, an OS error, a text codec failure) logs why and returns None so the caller passes the
# source through: ruff is off the deterministic-correctness path, and a give-up is a warning, never
# indistinguishable from "ruff found nothing to fix" (issue #159). The argv is a fixed list built in
# process (a resolved executable plus literal flags); the agent's source only travels on stdin,
# which is why S603 is a false positive here.
def run_ruff(argv: tuple[str, ...], code: str, ruff: RuffRun, *, step: str) -> RuffExit | None:
    try:
        proc = subprocess.run(  # noqa: S603
            argv,
            input=code,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=ruff.timeout_seconds,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, UnicodeError) as exc:
        failure = _failure_of(exc)
        assert failure is not None  # noqa: S101
        logger.warning(give_up_warning(step, len(code), failure, ruff.executable))
        return None
    return RuffExit(proc.returncode, proc.stdout)
