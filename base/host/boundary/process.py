# meiva:note
# The text a finished child process wrote.
#
# `Host.run` and `run_bounded` return `CompletedProcess[Any]`: whether `stdout` / `stderr` are text
# or bytes is decided by the caller's `text=`. A parameter typed `CompletedProcess[object]` accepts
# that without naming the `Any`, and these read the stream as the `str` a `text=True` caller asked
# for. A stream that was not captured is the empty string; a bytes stream is a `TypeError`, so a
# call that forgot `text=True` fails here instead of on a later `.strip()`. `stdout_bytes` /
# `stderr_bytes` are the same reading for a call that wants the raw bytes (no `text=`, so no newline
# translation) and fail on a text stream.

from __future__ import annotations

import subprocess

__all__ = ["stderr_bytes", "stderr_text", "stdout_bytes", "stdout_text"]


# meiva:note
# The captured stdout of `result` (a finished process, or the error `check=True` raised) as text ("" when it was not captured).
def stdout_text(result: subprocess.CompletedProcess[object] | subprocess.CalledProcessError) -> str:
    stream = result.stdout
    if stream is None:
        return ""
    if not isinstance(stream, str):
        raise TypeError(f"stdout is {type(stream).__name__}, not str (was text=True passed?)")
    return stream


# meiva:note
# The captured stderr of `result` (a finished process, or the error `check=True` raised) as text ("" when it was not captured).
def stderr_text(result: subprocess.CompletedProcess[object] | subprocess.CalledProcessError) -> str:
    stream = result.stderr
    if stream is None:
        return ""
    if not isinstance(stream, str):
        raise TypeError(f"stderr is {type(stream).__name__}, not str (was text=True passed?)")
    return stream


# meiva:note
# The captured stdout of `result` as the raw bytes a call without `text=` produced (b"" when it was not captured).
def stdout_bytes(
    result: subprocess.CompletedProcess[object] | subprocess.CalledProcessError,
) -> bytes:
    stream = result.stdout
    if stream is None:
        return b""
    if not isinstance(stream, bytes):
        raise TypeError(f"stdout is {type(stream).__name__}, not bytes (was text= passed?)")
    return stream


# meiva:note
# The captured stderr of `result` as the raw bytes a call without `text=` produced (b"" when it was not captured).
def stderr_bytes(
    result: subprocess.CompletedProcess[object] | subprocess.CalledProcessError,
) -> bytes:
    stream = result.stderr
    if stream is None:
        return b""
    if not isinstance(stream, bytes):
        raise TypeError(f"stderr is {type(stream).__name__}, not bytes (was text= passed?)")
    return stream
