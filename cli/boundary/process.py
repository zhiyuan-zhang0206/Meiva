# meiva:note
# The text a finished child process wrote.
#
# `Host.run` and `run_bounded` return `CompletedProcess[Any]`: whether `stdout` / `stderr` are text
# or bytes is decided by the caller's `text=`. A parameter typed `CompletedProcess[object]` accepts
# that without naming the `Any`, and these read the stream as the `str` a `text=True` caller asked
# for. A stream that was not captured is the empty string; a bytes stream is a `TypeError`, so a
# call that forgot `text=True` fails here instead of on a later `.strip()`.

from __future__ import annotations

import subprocess

__all__ = ["stderr_text", "stdout_text"]


# meiva:note
# The captured stdout of `result` as text ("" when it was not captured).
def stdout_text(result: subprocess.CompletedProcess[object]) -> str:
    stream = result.stdout
    if stream is None:
        return ""
    if not isinstance(stream, str):
        raise TypeError(f"stdout is {type(stream).__name__}, not str (was text=True passed?)")
    return stream


# meiva:note
# The captured stderr of `result` as text ("" when it was not captured).
def stderr_text(result: subprocess.CompletedProcess[object]) -> str:
    stream = result.stderr
    if stream is None:
        return ""
    if not isinstance(stream, str):
        raise TypeError(f"stderr is {type(stream).__name__}, not str (was text=True passed?)")
    return stream
