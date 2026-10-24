# meiva:note
# What `meiva schedules verify` concludes and prints, as values. The shell reads the stored rows,
# dry-imports each script in a child and emits the failure event; this module decides which rows
# are red, the exact lines the sweep prints (its line contract is parsed by the fleet update's
# pre-update check: one `RESULT` line, one `RED` line per red, `TOOL-ERROR` on exit 2), the exit
# code, and whether the sweep owes the alert pipeline a signal.
# meiva:link cli/docs/operator-surfaces/operator-surfaces.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple


class StoredSchedule(NamedTuple):
    schedule_id: int
    name: str
    script: str


class Red(NamedTuple):
    schedule_id: int
    name: str
    missing: str


@dataclass(frozen=True)
class SweepOutcome:
    lines: tuple[str, ...]
    rc: int
    checked: int
    reds: tuple[Red, ...]
    tool_error: str | None

    @property
    def owes_signal(self) -> bool:
        return self.tool_error is not None or bool(self.reds)


@dataclass(frozen=True)
class FailureEvent:
    checked: int
    red: int
    tool_error: str | None
    detail: str


# meiva:note
# A script with nothing in it (an empty or non-Python row) has nothing to dry-import: it is not a
# red.
def needs_check(script: str) -> bool:
    return bool(script.strip())


# meiva:note
# Each row paired with the child's verdict on its script: None is clean, or the row had nothing to
# check. Every row counts as checked, and a row with nothing to check counts as green.
def summarize(stamp: str, results: tuple[tuple[StoredSchedule, str | None], ...]) -> SweepOutcome:
    reds = tuple(
        Red(row.schedule_id, row.name, missing) for row, missing in results if missing is not None
    )
    rc = 1 if reds else 0
    lines = (
        f"RESULT ts={stamp} checked={len(results)} green={len(results) - len(reds)} "
        f"red={len(reds)} rc={rc}",
        *(f"RED id={red.schedule_id} name={red.name} missing={red.missing}" for red in reds),
    )
    return SweepOutcome(lines, rc, len(results), reds, None)


# meiva:note
# The rows could not be read at all: nothing was checked, which is its own exit code (2) and never
# a clean sweep.
def unreadable(stamp: str, error_type: str, message: str) -> SweepOutcome:
    detail = f"{error_type}: {message[:160]}"
    lines = (f"RESULT ts={stamp} checked=0 green=0 red=0 rc=2", f"TOOL-ERROR {detail}")
    return SweepOutcome(lines, 2, 0, (), detail)


# meiva:note
# The event a non-clean sweep emits. A Grafana rule reads it; a clean sweep emits nothing, so the
# alert resolves on its own once reds stop recurring.
def failure_event(outcome: SweepOutcome) -> FailureEvent:
    if outcome.tool_error is not None:
        detail = f"tool-error: {outcome.tool_error}"
    else:
        listing = "; ".join(
            f"id={red.schedule_id} name={red.name} missing={red.missing}" for red in outcome.reds
        )
        detail = (
            f"{len(outcome.reds)}/{outcome.checked} in-store schedule script(s) failed "
            f"dry-import: {listing}"
        )
    return FailureEvent(outcome.checked, len(outcome.reds), outcome.tool_error, detail)
