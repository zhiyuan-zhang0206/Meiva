# meiva:note
# What the daily CI metrics schedule decides: which day it reports and how many runs and failures
# that day holds. Whether a timed workflow is red is not decided here: `main-red-watch` reads every
# timed workflow's runs and owns that alarm (task #7006).
# meiva:link schedules/README.md

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import TypeGuard
from zoneinfo import ZoneInfo

__all__ = ["day_counts", "failure_message", "previous_day"]


def _is_str_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, Mapping)


# meiva:note
# The complete day before the slot, in the cluster timezone: the newest day the exporter's
# window is guaranteed to hold in full.
def previous_day(slot_end: datetime, tz: str) -> str:
    if slot_end.tzinfo is None:
        raise ValueError("slot_end must carry tzinfo")
    return (slot_end.astimezone(ZoneInfo(tz)).date() - timedelta(days=1)).isoformat()


def day_counts(snapshot: object, day: str, repo: str) -> tuple[int, int]:
    values = _field(_field(_field(_field(snapshot, "repositories"), repo), "days"), day)
    return int(str(_field(values, "runs"))), int(str(_field(values, "failed_runs")))


def _field(value: object, key: str) -> object:
    if not _is_str_mapping(value):
        raise TypeError(f"CI-run sampler snapshot: expected an object holding {key!r}")
    return value[key]


def failure_message(detail: str) -> str:
    return (
        f"Dev/CI metrics collection failed:\n{detail[-1000:]}\n"
        "Check the schedule log; backfill with `scripts/ci/pull_requests/runs_export.py --repo "
        "zhiyuan-zhang0206/Meiva --print-snapshot`."
    )
