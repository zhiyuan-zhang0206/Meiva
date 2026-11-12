from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from schedules.jobs.watch.release_gate_reads import read_scheduled_starts

NOW = datetime(2026, 11, 12, 5, tzinfo=UTC)


def row(identifier: int = 1, *, at: datetime = NOW) -> dict[str, object]:
    return {
        "id": identifier,
        "workflow_id": 400219585,
        "path": ".github/workflows/release-gate.yml",
        "event": "schedule",
        "head_branch": "main",
        "created_at": at.isoformat(),
    }


def response(rows: list[dict[str, object]], total: int | None = None) -> str:
    return json.dumps({"total_count": len(rows) if total is None else total, "workflow_runs": rows})


def read_pages(pages: list[str]) -> tuple[tuple[datetime, ...], list[str]]:
    calls: list[str] = []

    def read(path: str) -> str:
        calls.append(path)
        return pages[len(calls) - 1]

    return read_scheduled_starts("owner/repo", "release-gate.yml", branch="main", read=read), calls


def test_schedule_filter_and_full_timestamp_window_are_explicit() -> None:
    earlier = row(2, at=NOW - timedelta(hours=6))
    starts, calls = read_pages([response([row()], 100000), response([earlier, row()])])
    assert starts == (NOW, NOW - timedelta(hours=6))
    for path in calls:
        assert "/workflows/release-gate.yml/runs?" in path
        query = parse_qs(urlsplit(path).query)
        assert query["event"] == ["schedule"] and query["branch"] == ["main"]
    query = parse_qs(urlsplit(calls[1]).query)
    assert query["created"] == ["2026-11-11T05:00:00Z..2026-11-12T05:00:00Z"]
    assert query["per_page"] == ["100"]


def test_the_window_is_paginated_using_api_total_count() -> None:
    rows = [row(i + 1, at=NOW - timedelta(minutes=i)) for i in range(101)]
    starts, calls = read_pages(
        [response([rows[0]], 101), response(rows[:100], 101), response(rows[100:], 101)]
    )
    assert len(starts) == 101
    assert parse_qs(urlsplit(calls[-1]).query)["page"] == ["2"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("event", "workflow_dispatch"),
        ("event", "pull_request"),
        ("head_branch", "feature"),
        ("path", ".github/workflows/refresh-test-durations.yml"),
        ("created_at", "not-a-date"),
        ("created_at", "2026-11-12T05:00:00"),
    ],
)
def test_unrelated_or_invalid_metadata_is_unreadable(field: str, value: str) -> None:
    bad = {**row(), field: value}
    with pytest.raises(ValueError):
        _ = read_pages([response([row()]), response([bad])])


@pytest.mark.parametrize(
    "pages",
    [
        [response([])],
        [response([row()]), response([], 1)],
        [response([row()]), response([row()], 1000)],
        [response([row()]), response([row()], 101), response([], 101)],
        [response([row()]), response([row()], 101), response([row()], 102)],
        [response([row()]), response([row()], 101), response([row()], 101)],
        [response([row()]), response([row(2)])],
        [response([row()]), response([{**row(), "workflow_id": 123}])],
        [response([row()]), response([row(2, at=NOW - timedelta(hours=25))])],
        [response([row()]), response([row(2, at=NOW + timedelta(seconds=1))])],
    ],
)
def test_empty_capped_changed_truncated_and_unverified_windows_fail_closed(
    pages: list[str],
) -> None:
    with pytest.raises(ValueError):
        _ = read_pages(pages)


def test_documented_path_ref_suffix_is_accepted() -> None:
    actual = {**row(), "path": ".github/workflows/release-gate.yml@main"}
    starts, _ = read_pages([response([actual]), response([actual])])
    assert starts == (NOW,)
