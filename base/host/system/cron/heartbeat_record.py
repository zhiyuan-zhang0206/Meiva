# meiva:note
# Write the heartbeat a job definition asked for, on the way out of the process that ran it.
#
# The job writes it itself rather than a wrapper script emitting it: a wrapper is a second
# program the rendered definition has to name, it exists only where someone installed it, and its
# exit status stands in front of the real command's. Here the recorded code is the command's own,
# and the recorded pid is the process that did the work.
#
# Every exit writes. Recording only success would fold "ran and keeps failing" into "did not
# run", and those two have opposite fixes. When the record cannot be written, the run says so on
# stderr and appends the same reason to the degraded channel beside the heartbeat directory: the
# missing file is otherwise the only trace, and a job nobody watches leaves no stderr to read.
#
# A failed write does not replace the run's own exit code. That code is the primary fact — it is
# what tells an owner whether to investigate the job or the instrument — so it is kept, and 3 is
# reported only for the run whose own code was 0: "ok" is exactly the claim the missing record
# would have carried, and a success that could not be proved is not reported as one.

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from base.host.system.cron.heartbeat import exit_document
from base.host.system.job_values import ERROR_LOG_NAME, HEARTBEAT_ENV, HEARTBEAT_WRITE_FAILED_RC

__all__ = ["record_exit", "recording_exit"]


# meiva:note
# One append, not a rewrite: an error path that only adds a line succeeds where one that creates
# or truncates the file does not, and the first line of a degraded channel is worth more than its
# tidiness. A failure here changes nothing the run reports — stderr already carried the reason.
#
# The channel sits beside the heartbeat directory (`DIRECTORY_PARTS` in `job_values`), which the
# file this run failed to write names: `<home>/state/heartbeats/<name>.json` gives the log at
# `<home>/state/` plus `ERROR_LOG_NAME`.
def _append_error_line(*, log: Path, line: str) -> None:
    try:
        with log.open("a", encoding="utf-8") as handle:
            _ = handle.write(line + "\n")
    except OSError:
        return


# meiva:note
# Say why the record is missing in the two places that outlive this process, and return the code
# the process reports. The sentence names that code, so a reader of the log never has to work out
# whether the number it sees belongs to the job or to the instrument.
def _report_failure(*, target: Path, now: datetime, rc: int, reason: str) -> int:
    kept = rc if rc != 0 else HEARTBEAT_WRITE_FAILED_RC
    outcome = (
        f"job exit code {rc} preserved"
        if rc != 0
        else f"the run's success cannot be reported, exiting {HEARTBEAT_WRITE_FAILED_RC}"
    )
    print(f"meiva: heartbeat write failed ({reason}); {outcome}", file=sys.stderr)  # noqa: T201
    _append_error_line(
        log=target.parents[1] / ERROR_LOG_NAME,
        line=f"{now.isoformat()}\t{target.stem}\trc={rc}\t{reason}",
    )
    return kept


# meiva:note
# Write this run's heartbeat, returning the exit code the process should use: its own, and
# HEARTBEAT_WRITE_FAILED_RC when its own was 0 and the record could not be written.
def record_exit(*, environ: Mapping[str, str], pid: int, now: datetime, rc: int) -> int:
    target = environ.get(HEARTBEAT_ENV, "").strip()
    if not target:
        return rc
    path = Path(target)
    try:
        document = exit_document(name=path.stem, pid=pid, updated_at=now.timestamp(), rc=rc)
    except ValueError as exc:
        return _report_failure(
            target=path,
            now=now,
            rc=rc,
            reason=f"the job heartbeat {path} cannot be written as declared: {exc}",
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(document + "\n", encoding="utf-8")
    except OSError as exc:
        return _report_failure(
            target=path, now=now, rc=rc, reason=f"cannot write the job heartbeat {path}: {exc}"
        )
    return rc


# meiva:note
# A job command's exit, recorded. The three process facts are required parameters: each process
# entry reads its own environment, pid and clock (the CLI entry and the PR-flow script both
# already do) and hands them in, so nothing here reads the world and a test states the values it
# wants recorded instead of patching the process's.
#
# The exception paths re-raise, because the code that leaves the process is the job's: an
# uncaught error is the interpreter's 1, and a `SystemExit` carries the code the command chose.
# The one claim that cannot survive is `SystemExit(0)` over a record that could not be written —
# a run cannot report the success it failed to prove — so that exit is raised as
# HEARTBEAT_WRITE_FAILED_RC instead.
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
        job_code = exit_request.code if isinstance(exit_request.code, int) else 1
        reported = record_exit(environ=environment, pid=process, now=now(), rc=job_code)
        if reported == job_code:
            raise
        raise SystemExit(reported) from None
    except BaseException:
        _ = record_exit(environ=environment, pid=process, now=now(), rc=1)
        raise
    return record_exit(environ=environment, pid=process, now=now(), rc=rc)
