# meiva:note
# The data contracts of the CI run exporter: the per-repository cache file it keeps, the figures it computes per day and workflow, and the reconciliation snapshot it prints or persists.

from __future__ import annotations

from typing import NotRequired, TypedDict

from scripts.ci.pull_requests.runs_api import PullRecord, RunRecord

__all__ = [
    "Metrics",
    "RunCache",
    "Snapshot",
    "SnapshotRepository",
    "SnapshotRun",
    "SnapshotWindow",
]

# meiva:note
# One day's or one workflow's absolute figures: counts (`int`) and shares or percentiles (`float`).
Metrics = dict[str, int | float]


class RunCache(TypedDict):
    # meiva:note
    # One repository's cached GitHub records, keyed by stable id, and the watermark of the last fetch.

    runs: dict[str, RunRecord]
    prs: dict[str, PullRecord]
    last_fetch_end: str | None
    version: NotRequired[int]


class SnapshotWindow(TypedDict):
    days: int
    start_day: str
    end_day: str
    timezone: str


class SnapshotRun(TypedDict):
    window_days: int
    window_runs: int
    window_prs: int
    api_requests: int


class SnapshotRepository(TypedDict):
    days: dict[str, Metrics]
    workflows: dict[str, Metrics]
    run: SnapshotRun


class Snapshot(TypedDict):
    # meiva:note
    # The reconciliation JSON used by dry-run and the persistent snapshot file.

    version: int
    generated_at: str
    window: SnapshotWindow
    repositories: dict[str, SnapshotRepository]
