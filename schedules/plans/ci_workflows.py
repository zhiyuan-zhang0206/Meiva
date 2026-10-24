# meiva:note
# What the daily CI metrics schedule decides: which day it reports, how many runs and failures
# that day holds, and which timed workflows have been red long enough to wake the P0 lead.
#
# Timed workflows do not block a pull request, so a red one is silent by default: the nightly
# duration refresh failed 6 of 8 runs and nothing said so (task #5479), and the pricing refresh
# failed 10 in a row the same way (task #5496). Two consecutive failures is already "this
# pipeline is broken", not "one bad night". Each watched workflow carries what stays wrong while
# it is red, because the alert has to say what it costs.
# meiva:link schedules/README.md

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

__all__ = [
    "Alert",
    "WatchedWorkflow",
    "WorkflowRun",
    "consecutive_failures",
    "day_counts",
    "failure_message",
    "parse_workflow_runs",
    "plan_alert",
    "previous_day",
]

_CONSECUTIVE_FAILURES_ALERT = 2


@dataclass(frozen=True)
class WatchedWorkflow:
    file: str
    cost: str


# meiva:note
# A broken timed workflow: how many runs in a row failed and the message for the P0 lead.
@dataclass(frozen=True)
class Alert:
    streak: int
    message: str


@dataclass(frozen=True)
class WorkflowRun:
    conclusion: str | None
    html_url: str | None
    created_at: str | None


def parse_workflow_runs(payload: str, workflow: str) -> tuple[WorkflowRun, ...]:
    listing: object = json.loads(payload)
    runs = (
        cast(Mapping[str, object], listing).get("workflow_runs")
        if isinstance(listing, dict)
        else None
    )
    if not isinstance(runs, list):
        raise TypeError(f"{workflow}: the runs listing carried no workflow_runs")
    return tuple(_run(item, workflow) for item in cast(list[object], runs))


def _run(item: object, workflow: str) -> WorkflowRun:
    if not isinstance(item, dict):
        raise TypeError(f"{workflow}: a workflow run is not an object")
    fields = cast(Mapping[str, object], item)
    return WorkflowRun(
        _text(fields.get("conclusion")),
        _text(fields.get("html_url")),
        _text(fields.get("created_at")),
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


# meiva:note
# Newest first: a streak ends at the first run that did not fail. A run still in progress
# (conclusion None) is not a failure and must not inflate the streak.
def consecutive_failures(runs: Sequence[WorkflowRun]) -> int:
    streak = 0
    for run in runs:
        if run.conclusion != "failure":
            break
        streak += 1
    return streak


# meiva:note
# The alert that wakes the P0 lead, or None while the workflow is not broken yet.
def plan_alert(workflow: WatchedWorkflow, runs: Sequence[WorkflowRun]) -> Alert | None:
    streak = consecutive_failures(runs)
    if streak < _CONSECUTIVE_FAILURES_ALERT:
        return None
    newest = runs[0]
    return Alert(
        streak,
        f"Scheduled workflow {workflow.file} failed {streak} runs in a row. It does not "
        "block a pull request, so a red run is silent by default: while it keeps "
        f"failing, {workflow.cost}.\n"
        f"Newest run: {newest.html_url} ({newest.created_at})\n"
        "Find the failing step; the fix is accepted only after two consecutive "
        "successful nights.",
    )


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
    if not isinstance(value, Mapping):
        raise TypeError(f"CI-run sampler snapshot: expected an object holding {key!r}")
    return cast(Mapping[str, object], value)[key]


def failure_message(detail: str) -> str:
    return (
        f"Dev/CI metrics collection failed:\n{detail[-1000:]}\n"
        "Check the schedule log; backfill with `scripts/ci/pull_requests/runs_export.py --repo "
        "zhiyuan-zhang0206/Meiva --print-snapshot`."
    )
