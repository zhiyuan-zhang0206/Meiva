# meiva:note
# How a schedule script's end is worded and classified: values in, text and codes out.
#
# The engine runs the script and writes the outcome to ``schedules.last_error`` and
# ``schedule_runs``. What a ``SystemExit`` means, how a crash or a command failure reads in
# those columns, is decided here from plain values.

from __future__ import annotations

__all__ = [
    "command_failure_message",
    "command_timeout_message",
    "crash_note",
    "exit_code_of",
    "exit_detail",
]

# meiva:note
# A crashed run's note keeps this much of the traceback's END (the exception and the innermost
# frames): the session's output log is torn down with the session, and `schedules.last_error`
# holds only the latest crash, so the run row is the history of why each run died.
_NOTE_TRACEBACK_MAX = 3000


# meiva:note
# The process exit code a ``SystemExit`` argument stands for, with the interpreter's
# semantics: None is 0, booleans are their integer value, anything else (a message) is 1.
def exit_code_of(code: object) -> int:
    if isinstance(code, int):
        return int(code)
    return 0 if code is None else 1


# meiva:note
# The message a non-integer ``SystemExit`` argument would have printed, as a note suffix.
def exit_detail(code: object) -> str:
    return "" if code is None or isinstance(code, int) else f": {code}"


# meiva:note
# The run-history note of a crash: its exception and the end of its traceback.
def crash_note(exception_name: str, traceback_text: str) -> str:
    tail = traceback_text.strip()
    if len(tail) > _NOTE_TRACEBACK_MAX:
        tail = "[...truncated]\n" + tail[-_NOTE_TRACEBACK_MAX:]
    return f"crashed: {exception_name}\n{tail}"


def command_failure_message(returncode: int, command: str) -> str:
    return f"command exited {returncode}: {command!r}"


def command_timeout_message(timeout_s: float, command: str) -> str:
    return f"command did not finish within {timeout_s:.0f}s (stall timeout): {command!r}"
