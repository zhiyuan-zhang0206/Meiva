# meiva:note
# Write the heartbeat a job definition asked for, on the way out of the process that ran it.
#
# The job writes it itself rather than a wrapper script emitting it: a wrapper is a second
# program the rendered definition has to name, it exists only where someone installed it, and its
# exit status stands in front of the real command's. Here the recorded code is the command's own,
# and the recorded pid is the process that did the work.
#
# Every exit writes. Recording only success would fold "ran and keeps failing" into "did not
# run", and those two have opposite fixes — and a write that fails exits 3, so a run cannot
# report a success it was unable to prove. 3 never means "the command failed"; that is the
# command's own code, which the record carries beside it.

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from base.host.system.cron.heartbeat import exit_document
from base.host.system.job_values import HEARTBEAT_ENV, HEARTBEAT_WRITE_FAILED_RC

__all__ = ["record_exit", "recording_exit"]


# meiva:note
# A run that cannot prove itself says so on stderr and reports the write failure, never silence:
# the definition's own log is the only place an operator sees why the file is missing.
def _refuse(message: str) -> int:
    print(f"meiva: {message}", file=sys.stderr)  # noqa: T201
    return HEARTBEAT_WRITE_FAILED_RC


# meiva:note
# Write this run's heartbeat, returning the exit code the process should use: the command's own
# on success, and 3 when the record could not be written at all.
def record_exit(*, environ: Mapping[str, str], pid: int, now: datetime, rc: int) -> int:
    target = environ.get(HEARTBEAT_ENV, "").strip()
    if not target:
        return rc
    path = Path(target)
    try:
        document = exit_document(name=path.stem, pid=pid, updated_at=now.timestamp(), rc=rc)
    except ValueError as exc:
        return _refuse(f"the job heartbeat {path} cannot be written as declared: {exc}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(document + "\n", encoding="utf-8")
    except OSError as exc:
        return _refuse(f"cannot write the job heartbeat {path}: {exc}")
    return rc


# meiva:note
# A job command's exit, recorded. The three process facts are required parameters: each process
# entry reads its own environment, pid and clock (the CLI entry and the PR-flow script both
# already do) and hands them in, so nothing here reads the world and a test states the values it
# wants recorded instead of patching the process's.
def recording_exit(
    run: Callable[[], int],
    *,
    environ: Mapping[str, str],
    pid: int,
    clock: Callable[[], datetime],
) -> int:
    environment, process, now = environ, pid, clock
    try:
        rc = run()
    except SystemExit as exit_request:
        _ = record_exit(
            environ=environment,
            pid=process,
            now=now(),
            rc=exit_request.code if isinstance(exit_request.code, int) else 1,
        )
        raise
    except BaseException:
        _ = record_exit(environ=environment, pid=process, now=now(), rc=1)
        raise
    return record_exit(environ=environment, pid=process, now=now(), rc=rc)
