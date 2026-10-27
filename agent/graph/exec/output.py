"""Code execution output envelope: format / truncate / overflow-to-file.

The exec node (`node.py`) runs agent code and collects its merged stdout+stderr
stream; this module turns that raw stream into the "Code execution output:"
envelope fed back to the LLM. Split out from `node.py` to keep that file's line
budget — this is a self-contained formatting layer with one public entry,
`wrap_code_output`, called by the exec node's result dispatch.

Long multiline output can first use a configurable line preview backed by a
full archive protected while the current context references it (`_crop`).
The existing hard inline cap uses a separate 20-file ring under `.exec_output/`
in the agent's workspace. Neither archive lives in the OS temporary directory.

The hard inline limit is the SECOND of two hard caps. The first (`_stream.StreamingTextIO`)
bounds accumulation while the code is still running; when it fired, its
`StreamCap` travels here so this layer reports the true produced length and
stops promising the archive holds the full text — the middle was dropped before
the archive was ever written. The accumulation budget is validated to be >=
`exec_output_max_chars`, so `truncate_both_ends` always slices its head out of
the accumulator's kept head and its tail out of the kept tail.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from langchain_core.messages import BaseMessage

from base.agents.messages.inbound import InterruptReason
from base.log import logger
from base.paths import workspace_dir_readonly

from ._crop import CropLimits, crop_output
from ._result import StreamCap

# The default marker is the user Stop action. Maintenance and other external
# system interruptions retain their observed attribution in the same envelope.
EXEC_CANCEL_NOTE = "[cancelled by user]"

# Overflow output is written into the agent's workspace so the agent can read /
# grep the complete output even when only head + tail are shown inline — and it
# survives OS temp-dir reaping and reboots, so day-old output stays greppable.
# Sibling of `.shell_logs/` (background-run logs): both live in the workspace,
# the default base of the agent's file and shell tools.
_OVERFLOW_DIRNAME = ".exec_output"
# Ring: keep the most recent N overflow files per workspace, prune older ones
# so the dir doesn't grow unbounded across a long-lived agent.
OVERFLOW_KEEP = 20


def overflow_dir(agent_id: int, *, home: Path) -> Path:
    return workspace_dir_readonly(agent_id, home=home) / _OVERFLOW_DIRNAME


# meiva:note
# Everything the envelope reads from configuration and the workspace, as values: where the
# archives live, the soft-crop view, the timeout the marker names and the inline cap.
@dataclass(frozen=True)
class OutputLimits:
    archive_dir: Path
    crop: CropLimits
    timeout_seconds: float
    max_chars: int


class ExecEnvelope(NamedTuple):
    """The text fed to the LLM and where its body begins (`meiva_exec_body_start`)."""

    text: str
    body_start: int


def format_elapsed(seconds: float) -> str:
    """Wall-clock duration as the agent reads it: `850ms`, `4.2s`, `53s`, `1min 53s`, `2h 3min 4s`."""
    if seconds < 1:
        return f"{round(seconds * 1000)}ms"
    if seconds < 10:
        return f"{seconds:.1f}s"
    whole = round(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}min {secs}s"
    if minutes:
        return f"{minutes}min {secs}s"
    return f"{secs}s"


def _marker(
    *, cancelled: bool, cancel_reason: InterruptReason, timed_out: bool, timeout_seconds: float
) -> str:
    # cancelled and timed_out are mutually exclusive — the subprocess result
    # constructor guarantees only one is set. Assert is defense-in-depth: if a future caller
    # forgets the mutual exclusion, this blows up rather than silently emitting two markers.
    assert not (cancelled and timed_out), "cancelled and timed_out are mutually exclusive"  # noqa: S101
    if cancelled:
        match cancel_reason:
            case InterruptReason.USER | InterruptReason.SYSTEM:
                return f" [cancelled by {cancel_reason.value}]"
            case InterruptReason.RESTART:
                return " [interrupted by restart]"
    if timed_out:
        return f" [timeout after {timeout_seconds:.0f}s]"
    return ""


def _fit_body(
    output: str,
    stream_cap: StreamCap | None,
    referenced_messages: Sequence[BaseMessage],
    limits: OutputLimits,
    now: datetime,
) -> str:
    """The envelope body for non-empty output: logged, soft-cropped or truncated to fit."""
    # Log the true (pre-truncation) length on every exec — instrumentation
    # for tuning max_chars from a real distribution (the stored output is
    # capped, so length can't be recovered from history afterwards). When
    # the accumulation cap fired, `len(output)` is already the capped
    # length; the StreamCap carries what the code actually produced.
    produced = stream_cap.produced_chars if stream_cap is not None else len(output)
    logger.info("[exec output chars] {n}", n=produced)
    preview = None
    crop = limits.crop
    if stream_cap is None and crop.exec_output_crop_after_lines:
        over = (
            len(output.splitlines()) > crop.exec_output_crop_after_lines
            or len(output) > crop.exec_output_crop_after_chars
            or len(output.encode("utf-8")) > crop.exec_output_crop_after_bytes
        )
        if over:
            preview = crop_output(
                output,
                limits.archive_dir,
                crop,
                referenced_messages=referenced_messages,
                max_chars=limits.max_chars,
            )
    if preview is not None:
        output = preview
    elif len(output) > limits.max_chars:
        output = truncate_both_ends(
            output,
            limits.max_chars,
            archive_dir=limits.archive_dir,
            now=now,
            stream_cap=stream_cap,
        )
    return output if output.endswith("\n") else output + "\n"


# meiva:note
# Generate the code execution output envelope fed to the LLM.
#
# `output` is the exec's stdout + stderr merged stream (both redirected to the same
# `StreamingTextIO`) — preserves the real chronological order of print / traceback / log
# output, same as running Python in a terminal.
#
# When cancelled=True, names the observed user/system reason. Partial output is retained;
# cancellation does not imply that external effects were undone.
#
# The caller supplies `limits` (the cluster sandbox limits, the crop view and the archive
# directory), `now` (the instant the overflow archive is stamped with, in the configured
# timezone) and the optional rendered timestamp. This layer never discovers configuration.
#
# `elapsed_seconds` is the run's wall-clock time (also for cancelled and timed-out runs: the
# time before the stop); the header always carries it, with or without a timestamp.
#
# `stream_cap` is set when the accumulation cap already dropped the middle of `output`
# mid-run: it carries the true produced length for the instrumentation log line, and tells
# the overflow archive to stop claiming it holds the full text.
#
# `referenced_messages` is the native node's current context, passed explicitly because the
# parent does not install that state into the child SDK's slot. Soft-crop archives mentioned
# there cannot be evicted to make space.
#
# Format (double \n after header, same contract as wrap_inbound; the frontend splitEnvelope
# uses this to split header / body):
#
#     Code execution output after running for 1min 53s [timestamp]:
#
#     <output>
#     (no output)          # only when output is empty — explicitly tells the agent code ran but produced no output
def wrap_code_output(
    output: str,
    *,
    limits: OutputLimits,
    now: datetime,
    elapsed_seconds: float,
    timestamp: str | None = None,
    cancelled: bool = False,
    cancel_reason: InterruptReason = InterruptReason.USER,
    timed_out: bool = False,
    stream_cap: StreamCap | None = None,
    referenced_messages: Sequence[BaseMessage] = (),
) -> ExecEnvelope:
    marker = _marker(
        cancelled=cancelled,
        cancel_reason=cancel_reason,
        timed_out=timed_out,
        timeout_seconds=limits.timeout_seconds,
    )
    ts = f" {timestamp}" if timestamp is not None else ""
    header = (
        f"Code execution output after running for {format_elapsed(elapsed_seconds)}{marker}{ts}:"
    )
    if not output:
        body = "(no output)"
    else:
        body = _fit_body(output, stream_cap, referenced_messages, limits, now)
    if timed_out:
        # The envelope is the agent's only cue to change strategy — the better
        # primitives must be named here, where the timeout is actually seen.
        body += (
            "\n[hint: this budget is for quick work. Run a long shell command "
            "with meiva.shell.run_background (delivers a completion message), or "
            "long Python code as meiva.watcher.launch; drive interactive programs "
            "in meiva.shell.sessions.]\n"
        )
    head = header + "\n\n"
    return ExecEnvelope(head + body, len(head))


def truncate_both_ends(
    output: str,
    max_chars: int,
    *,
    archive_dir: Path,
    now: datetime,
    stream_cap: StreamCap | None = None,
) -> str:
    """Keep the first + last `max_chars // 2` chars (head carries an overview /
    a help() listing, tail carries the result / traceback), drop the middle,
    and write the archived text to a file the agent can read or grep.

    With `stream_cap` set, `output` is itself the accumulation-capped text: the
    archive holds only what survived that first cap, so the banner says exactly
    that instead of promising the full output.

    Public because the meiva_code plugin reuses the same truncate-and-archive
    logic for oversized context files (AGENTS.md / CLAUDE.md) — the user
    ruling (2026-08-11) prescribes the exec-output overflow shape for any
    content too large to fully inject: inline head+tail + archive path."""
    half = max_chars // 2
    head, tail = output[:half], output[-half:]
    omitted = len(output) - 2 * half
    path = _write_overflow_file(output, archive_dir=archive_dir, now=now, stream_cap=stream_cap)
    if stream_cap is None:
        total = f"{len(output):,} chars total"
        source = f"full output at {path}"
    else:
        total = (
            f"{stream_cap.produced_chars:,} chars produced, capped to "
            f"{stream_cap.budget_chars:,} during execution — the dropped middle "
            f"is unrecoverable"
        )
        source = f"surviving output at {path}"
    banner = (
        f"[output truncated: {total}; {source} "
        f"(read or grep it); showing first {half:,} + last {half:,} chars]"
    )
    mid = f"\n\n[... {omitted:,} chars omitted — {source} ...]\n\n"
    return banner + "\n\n" + head + mid + tail


def _write_overflow_file(
    output: str, *, archive_dir: Path, now: datetime, stream_cap: StreamCap | None = None
) -> Path:
    """Write `output` to a timestamped file under the workspace overflow dir,
    prune old ones, and return its path.

    A `stream_cap` means the text was already truncated at accumulation time,
    so the file gets a header saying it is not the full output — the agent
    grepping it must not read a miss as "the string was never printed".

    The workspace is per-agent, so no agent-id prefix is needed; the
    microsecond timestamp makes names sortable by exec time and unique within
    an agent (execs are serial, and two in the same microsecond cannot happen).
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f"exec_{now.strftime('%Y%m%d_%H%M%S_%f')}.txt"
    if stream_cap is not None:
        output = (
            f"[archive note: {stream_cap.produced_chars:,} chars were produced; the "
            f"{stream_cap.budget_chars:,}-char accumulation budget kept the first + last "
            f"halves and dropped the middle DURING execution — this file is NOT the "
            f"full output]\n\n"
        ) + output
    path.write_text(output, encoding="utf-8")
    existing = sorted(archive_dir.glob("exec_*.txt"), key=lambda p: p.stat().st_mtime)
    for old in existing[:-OVERFLOW_KEEP]:
        old.unlink(missing_ok=True)
    return path
