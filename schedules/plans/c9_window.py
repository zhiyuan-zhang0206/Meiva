# meiva:note
# What the daily CI-minute reconciliation reports, from the claimed slot and the ledger rows.
#
# The window is slot-based, not trailing-now: a late catch-up fire still covers its own
# 05:00-05:00 day, and the day label is the slot's cluster-timezone date, which can differ from
# its UTC date. The minute rates are the shell's: `scripts/ci/pull_requests/accounting.py`
# owns them and the shell hands them in.
# meiva:link schedules/docs/c9-daily-report.meiva.okf.md

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

__all__ = ["CiRun", "MinuteRates", "failure_message", "summarize", "window_bounds"]


# meiva:note
# One reconciled run: the agent its PR title names (or none) and the minutes it billed.
@dataclass(frozen=True)
class CiRun:
    agent_id: int | None
    linux_minutes: int
    macos_minutes: int


@dataclass(frozen=True)
class MinuteRates:
    linux_usd: float
    macos_usd: float


def window_bounds(slot_end: datetime, tz: str) -> tuple[str, str, str]:
    if slot_end.tzinfo is None:
        raise ValueError("slot_end must carry tzinfo")
    since = slot_end - timedelta(hours=24)
    day = slot_end.astimezone(ZoneInfo(tz))
    return (
        since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        slot_end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        day.strftime("%Y-%m-%d"),
    )


def summarize(
    runs: Sequence[CiRun],
    *,
    day: str,
    window_start: str,
    window_end: str,
    appended_runs: int,
    rates: MinuteRates,
) -> dict[str, str | int | float]:
    attributed = [run for run in runs if run.agent_id is not None]
    linux_minutes = sum(run.linux_minutes for run in runs)
    macos_minutes = sum(run.macos_minutes for run in runs)
    return {
        "day": day,
        "window_start": window_start,
        "window_end": window_end,
        "runs": len(runs),
        "attributed_runs": len(attributed),
        "unattributed_runs": len(runs) - len(attributed),
        "total_minutes": linux_minutes + macos_minutes,
        "attributed_minutes": sum(run.linux_minutes + run.macos_minutes for run in attributed),
        "linux_minutes": linux_minutes,
        "macos_minutes": macos_minutes,
        "appended_runs": appended_runs,
        "est_usd": round(
            linux_minutes * rates.linux_usd + macos_minutes * rates.macos_usd,
            2,
        ),
    }


def failure_message(detail: str) -> str:
    return (
        f"C9 daily reconciliation failed:\n{detail[-1000:]}\n"
        "Check the schedule log; backfill the missed window manually with "
        "`scripts/ci/pull_requests/accounting.py --since ... --until ... --append-ledger`."
    )
