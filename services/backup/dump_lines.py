# meiva:note
# The log and progress lines of a logical dump, as values in and strings out.

from __future__ import annotations

from pathlib import Path

__all__ = ["beat_line", "restore_failure_message", "size_breakdown", "started_line"]

_MIB = 2**20


# meiva:note
# One-line DB composition for the backup log: total, the LangGraph checkpoint tables,
# and everything else. The checkpoint tables dominate DB size and dump time.
def size_breakdown(total: int, blobs: int, checkpoints: int, writes: int) -> str:
    checkpoint = blobs + checkpoints + writes
    rest = max(total - checkpoint, 0)
    return (
        f"db={round(total / _MIB)}MiB checkpoint={round(checkpoint / _MIB)}MiB "
        f"rest={round(rest / _MIB)}MiB"
    )


# meiva:note
# The first progress line of a stage: its label and its bound in minutes.
def started_line(label: str, timeout_s: float) -> str:
    return f"{label} started (bounded at {timeout_s / 60:.0f} min)"


# meiva:note
# The `, N MiB written` evidence of a child's output file; empty when it cannot be read.
def _written_suffix(written_bytes: int | None) -> str:
    if written_bytes is None:
        return ""
    return f", {written_bytes / _MIB:.1f} MiB written"


# meiva:note
# A heartbeat line: the stage label, the elapsed seconds, and the written-bytes evidence.
def beat_line(label: str, elapsed_s: float, written_bytes: int | None) -> str:
    return f"{label} {elapsed_s:.0f}s{_written_suffix(written_bytes)}"


# meiva:note
# The `pg_restore` failure with the capacity context an operator needs.
#
# A scratch restore that outgrows its base dies server-side mid-COPY, so the client-side
# failure carries no diagnosis of its own: the 2026-09-14 run surfaced only `pg_restore
# exited 1` while the postmaster had been killed by the full tmpfs. Naming the base and
# its free space makes the next occurrence diagnosable from the drill's own output.
def restore_failure_message(returncode: int, stderr: bytes | None, base: Path, free: str) -> str:
    hint = (
        f"pg_restore exited {returncode} against the throwaway cluster on {base} "
        f"({free} free). If the server closed "
        f"the connection mid-copy, the scratch base ran out of room — free space on "
        f"{base} or point MEIVA_PG_THROWAWAY_BASE at a larger volume."
    )
    stderr_tail = "\n".join((stderr or b"").decode(errors="replace").splitlines()[-5:])
    return f"{hint}\npg_restore stderr tail:\n{stderr_tail}" if stderr_tail else hint
