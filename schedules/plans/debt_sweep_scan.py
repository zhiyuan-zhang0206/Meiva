# meiva:note
# What the daily mechanical scan reported and where its report is kept.
#
# The shell runs `scripts/audit/tech_debt_candidates.sh` and hands the outcome here: the report
# text is the program's stdout then stderr, with a final line naming a non-zero exit, and a scan
# that could not run at all (a missing shell, a timeout) is a failed scan with its error as the
# text. The day label is the slot's date in the cluster timezone, which can differ from its UTC
# date, and the report is stored under that day.
# meiva:link schedules/docs/debt-sweep.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

__all__ = [
    "ScanExited",
    "ScanOutcome",
    "ScanReport",
    "ScanUnrunnable",
    "scan_artifact_path",
    "scan_report",
    "slot_day",
]

_SUMMARY_TAIL = 240


# meiva:note
# The scan ran and exited with `returncode`; `output` is its stdout followed by its stderr.
@dataclass(frozen=True)
class ScanExited:
    returncode: int
    output: str


# meiva:note
# The scan could not run to completion; `error` names the exception and its message.
@dataclass(frozen=True)
class ScanUnrunnable:
    error: str


ScanOutcome = ScanExited | ScanUnrunnable


@dataclass(frozen=True)
class ScanReport:
    succeeded: bool
    summary: str
    text: str


def _summary(*, succeeded: bool, text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    outcome = "completed" if succeeded else "failed"
    final_line = lines[-1] if lines else "no output"
    return f"mechanical scan {outcome} ({len(lines)} lines; final: {final_line[:_SUMMARY_TAIL]})"


def scan_report(outcome: ScanOutcome) -> ScanReport:
    if isinstance(outcome, ScanUnrunnable):
        text = f"mechanical scan could not run: {outcome.error}\n"
        return ScanReport(succeeded=False, summary=_summary(succeeded=False, text=text), text=text)
    succeeded = outcome.returncode == 0
    text = outcome.output
    if not succeeded:
        text += f"\nmechanical scan exited {outcome.returncode}\n"
    return ScanReport(succeeded, _summary(succeeded=succeeded, text=text), text)


def slot_day(slot: datetime, tz: str) -> str:
    if slot.tzinfo is None:
        raise ValueError("slot must carry tzinfo")
    return slot.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")


def scan_artifact_path(home: Path, day: str, slot: datetime) -> Path:
    if slot.tzinfo is None:
        raise ValueError("slot must carry tzinfo")
    stamp = slot.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return home / "debt-sweeps" / day / f"mechanical-scan-{stamp}.txt"
