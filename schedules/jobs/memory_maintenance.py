# meiva:note
# Wake the memory arbiter for one of its two daily jobs. The 12:00 merge trigger was removed on
# 2026-08-01: merge, close and revert are the merge captain's job, not the arbiter's. A failure
# is logged and swallowed, so one unreachable agent does not stop the next day's slots.
# meiva:link schedules/README.md

from __future__ import annotations

from datetime import datetime

from schedules.jobs.agent_directory import ensure_agent

__all__ = ["wake_arbiter"]


def wake_arbiter(_slot: datetime, message: str | None, *, label: str) -> None:
    if message is None:
        raise ValueError("a memory maintenance trigger carries its message")
    print(f"[schedule] Firing: {message}")
    try:
        ensure_agent(label, f"memory-arbiter: {message}")
        print(f"[schedule] Agent notified: {message}")
    except Exception as exc:
        print(f"[schedule] Failed: {exc}")
