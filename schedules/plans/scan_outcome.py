# meiva:note
# What the daily self-evolution schedule does with the scan's outcome: what it logs, whether it
# wakes the self-evolution agent, and what it sends the daily report recipient.
#
# `daily_scan.py` exits 0 when the day is clean and 2 when it found something to act on, so a
# missing script must not masquerade as an ALERT: python also exits 2 when it cannot open the
# file. The shell checks the file first and passes `ScanMissing` instead of running it. A
# timeout and a missing script wake the agent but send no daily report; every finished scan
# sends one, whatever its exit code.
# meiva:link schedules/docs/self-evolution.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import assert_never

__all__ = [
    "ScanMissing",
    "ScanPlan",
    "ScanRan",
    "ScanResult",
    "ScanTimedOut",
    "parse_daily_report_agent",
    "plan_scan",
]

DEFAULT_REPORT_AGENT = 228


@dataclass(frozen=True)
class ScanRan:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ScanTimedOut:
    seconds: int


@dataclass(frozen=True)
class ScanMissing:
    path: str


ScanResult = ScanRan | ScanTimedOut | ScanMissing


# meiva:note
# `log_lines` are printed in order, `wake_prompt` is sent to the self-evolution agent when it is
# not None, and `daily_report` goes to the report recipient when it is not None.
@dataclass(frozen=True)
class ScanPlan:
    log_lines: tuple[str, ...]
    wake_prompt: str | None
    daily_report: str | None


def plan_scan(result: ScanResult, now: datetime) -> ScanPlan:
    stamp = now.isoformat()
    match result:
        case ScanMissing(path=path):
            message = (
                f"daily_scan.py missing at {path} — run `meiva skill update meiva-self-evolution` "
                f"(or `meiva skill update` for all repo-native skills) to refresh the load-dir copy"
            )
            return ScanPlan((f"[{stamp}] {message}",), f"Daily scan cannot run: {message}", None)
        case ScanTimedOut(seconds=seconds):
            return ScanPlan(
                (f"scan timed out after {seconds}s",),
                f"Daily scan timed out after {seconds}s — check whether daily_scan.py is stuck.",
                None,
            )
        case ScanRan():
            return _plan_finished_scan(result, now)
        case _:
            assert_never(result)


def _plan_finished_scan(result: ScanRan, now: datetime) -> ScanPlan:
    stamp = now.isoformat()
    tail = result.stdout[-2000:]
    err = result.stderr[-500:]
    log_lines = [f"[{stamp}] scan rc={result.returncode}", tail]
    wake: str | None = None
    if result.returncode == 2:
        wake = f"Daily scan ALERT ({stamp}):\n{tail}\n{err}\nReview the daily report and act."
    elif result.returncode != 0:
        log_lines.append(f"scan failed rc={result.returncode}: {err}")
        wake = (
            f"Daily scan failed rc={result.returncode}:\n{err}\n"
            "Check the schedule log and daily_scan.py."
        )
    report = f"[self-evolution daily {now.strftime('%m-%d')}]\n{tail}"
    return ScanPlan(tuple(log_lines), wake, report)


# meiva:note
# `MEIVA_SELF_EVOLUTION_DAILY_REPORT_AGENT`: unset means the default recipient, an explicitly
# empty value skips the daily report, anything else must be a positive agent id.
def parse_daily_report_agent(raw: str | None) -> int | None:
    if raw is None:
        return DEFAULT_REPORT_AGENT
    if not raw.strip():
        return None
    try:
        agent_id = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            "MEIVA_SELF_EVOLUTION_DAILY_REPORT_AGENT must be a numeric agent id"
        ) from exc
    if agent_id <= 0:
        raise RuntimeError("MEIVA_SELF_EVOLUTION_DAILY_REPORT_AGENT must be a positive agent id")
    return agent_id
