"""Contract tests for the GitHub Actions run-observability exporter."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from base.clock.tests.manual_timers import ManualTimers
from base.telemetry import EventPipeline
from scripts.ci.pull_requests import runs_api, runs_export
from scripts.ci.pull_requests.export_process import GhProcess


def _run(**overrides: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": 1,
        "workflow_id": 7,
        "name": "CI",
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
        "head_branch": "meiva-1-demo",
        "head_sha": "a" * 40,
        "created_at": "2026-09-02T10:00:00Z",
        "run_started_at": "2026-09-02T10:00:00Z",
        "updated_at": "2026-09-02T10:05:00Z",
    }
    row.update(overrides)
    return row


def _pr(**overrides: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "number": 1,
        "head_ref": "meiva-1-demo",
        "head_sha": "a" * 40,
        "created_at": "2026-09-02T08:00:00Z",
        "updated_at": "2026-09-02T12:00:00Z",
        "merged_at": "2026-09-02T12:00:00Z",
        "closed_at": "2026-09-02T12:00:00Z",
    }
    row.update(overrides)
    return row


def _gh_queue(responses: list[tuple[str, str, int]]) -> GhProcess:
    def run(
        command: Sequence[str], *, timeout: float, capture_output: bool, text: bool
    ) -> subprocess.CompletedProcess[str]:
        assert timeout == 60 and capture_output and text
        assert list(command[:4]) == ["gh", "api", "--method", "GET"]
        joined = " ".join(command)
        for index, (key, output, code) in enumerate(responses):
            if key in joined:
                responses.pop(index)
                return subprocess.CompletedProcess(
                    command, code, output, (output or "failure") if code else ""
                )
        raise AssertionError(f"unmatched: {joined}")

    return run


def test_created_cursor_walk_deduplicates_run_ids() -> None:
    page = [_run(id=9, created_at="2026-09-02T00:00:00Z")] * 100
    commands: list[str] = []

    def record(
        command: Sequence[str], *, timeout: float, capture_output: bool, text: bool
    ) -> subprocess.CompletedProcess[str]:
        assert timeout == 60 and capture_output and text
        commands.append(" ".join(command))
        output = json.dumps(page if len(commands) == 1 else [])
        return subprocess.CompletedProcess(command, 0, output, "")

    rows = runs_api.fetch_runs(
        "owner/repo",
        datetime(2026, 9, 1, tzinfo=UTC),
        datetime(2026, 9, 3, tzinfo=UTC),
        runs_api.FetchStats(),
        run_process=record,
        timers=ManualTimers(),
        max_pages=1,
    )

    assert [row["id"] for row in rows] == [9]
    assert "created=%3C%3D2026-09-03T00:00:00Z" in commands[0]
    assert "created=%3C%3D2026-09-01T23:59:59Z" in commands[1]


def test_classification_rules_cover_noise_white_retry_and_trunk_mapping() -> None:
    module = runs_export
    instant = _run(conclusion="skipped", updated_at="2026-09-02T10:00:02Z")
    cancelled = _run(id=2, conclusion="cancelled", updated_at="2026-09-02T10:00:10Z")
    replacement = _run(id=3, created_at="2026-09-02T10:00:05Z")
    zombie = _run(id=4, updated_at="2026-09-02T16:00:01Z")
    failed_retry = _run(id=5, conclusion="failure", run_attempt=2)

    assert module.is_instant_skip(instant)
    assert module.superseded_ids([cancelled, replacement]) == {2}
    assert module.is_zombie(zombie)
    assert module.is_failed(failed_retry)
    assert module.workflow_class("Runner shutdown rerun") == "watchdog"
    assert module.workflow_class("CI failed-job rerun") == "watchdog"
    assert module.workflow_class("Example proof") == "proof"
    assert module.workflow_class("Release desktop") == "release"
    assert module.trunk_merge_pr_number("trunk-merge/pr-42/a4e9") == 42
    assert module.trunk_merge_pr_number("meiva-42-demo") is None


def test_attribute_runs_respects_pr_life_and_synthetic_trunk_branch() -> None:
    module = runs_export
    pull = _pr(number=42, head_ref="feature", created_at="2026-09-02T08:00:00Z")
    matched = _run(id=1, head_branch="feature", created_at="2026-09-02T07:00:00Z")
    early = _run(id=2, head_branch="feature", created_at="2026-09-02T05:59:59Z")
    trunk = _run(id=3, head_branch="trunk-merge/pr-42/deadbeef", created_at="2026-09-02T11:00:00Z")

    assert module.attribute_runs([matched, early, trunk], [pull]) == {1: 42, 3: 42}


def test_daily_aggregates_deduplicate_white_runs_and_pin_first_pass() -> None:
    module = runs_export
    day = date(2026, 9, 2)  # time-bomb-ok: fixture pins one complete cluster-tz day.
    runs = [
        _run(id=1, conclusion="skipped", updated_at="2026-09-02T10:00:02Z"),
        _run(id=2, conclusion="cancelled", updated_at="2026-09-02T10:00:10Z"),
        _run(id=3, created_at="2026-09-02T10:05:10Z"),
        _run(id=4, conclusion="failure", run_attempt=2),
        _run(id=5, conclusion="success", run_attempt=2),
        _run(id=6, head_sha="b" * 40, head_branch="closed", created_at="2026-09-02T10:20:00Z"),
        _run(id=7, name="Runner shutdown rerun", created_at="2026-09-02T10:30:00Z"),
    ]
    prs = [
        _pr(),
        _pr(
            number=2,
            head_ref="closed",
            head_sha="b" * 40,
            merged_at=None,
            closed_at="2026-09-02T12:00:00Z",
        ),
    ]

    result = module.daily_aggregates(runs, prs, [day], ZoneInfo("UTC"))["2026-09-02"]

    assert result["runs"] == 7
    assert result["instant_skip_runs"] == 1
    assert result["superseded_runs"] == 1
    assert result["failed_runs"] == 1
    assert result["retried_failed_runs"] == 1
    assert result["self_healed_runs"] == 1
    assert result["abandoned_runs"] == 1
    assert result["watchdog_runs"] == 1
    assert result["white_run_share"] == 0.429
    assert result["prs_completed"] == 2
    assert result["prs_with_runs"] == 2
    assert result["first_pass_pr_share"] == pytest.approx(1 / 2)


def test_workflow_window_uses_non_zombie_execution_and_pr_appearance() -> None:
    module = runs_export
    runs = [
        _run(id=1, updated_at="2026-09-02T10:00:04Z"),
        _run(id=2, updated_at="2026-09-02T16:00:01Z"),
        _run(id=3, conclusion="failure", run_attempt=2),
    ]
    values = module.workflow_aggregates(runs, [_pr()])["CI"]

    assert values["runs"] == 3
    assert values["failed_runs"] == 1
    assert values["retried_failed_runs"] == 1
    assert values["prs_appeared_on"] == 1
    assert values["pr_appearance_share"] == 1
    assert values["exec_median_seconds"] == 152
    assert values["exec_p90_seconds"] == 270.4


def test_cache_merge_is_idempotent_prunes_old_records_and_moves_watermark() -> None:
    module = runs_export
    fresh = _run(id=1, updated_at="2026-09-02T11:00:00Z")
    old = _run(id=2, created_at="2026-08-01T10:00:00Z")
    cache = {"runs": {"1": _run(id=1), "2": old}, "prs": {"1": _pr()}, "last_fetch_end": None}
    merged = module.merge_cache(
        cache,
        [fresh],
        [_pr(number=3)],
        cache_start=datetime(2026, 9, 1, tzinfo=UTC),
        fetched_end=datetime(2026, 9, 3, tzinfo=UTC),
    )

    assert set(merged["runs"]) == {"1"}
    assert merged["runs"]["1"]["updated_at"] == "2026-09-02T11:00:00Z"
    assert set(merged["prs"]) == {"1", "3"}
    assert merged["last_fetch_end"] == "2026-09-03T00:00:00+00:00"


def test_dry_run_prints_snapshot_without_writes_or_emission(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    replies = [("actions/runs?", "[]", 0), ("pulls?", "[]", 0)]
    observed: list[object] = []
    pipeline = EventPipeline(writer=observed.extend)
    try:
        assert (
            runs_export.main(
                ["--repo", "owner/repo", "--state-dir", str(tmp_path), "--dry-run"],
                run_process=_gh_queue(replies),
                timers=ManualTimers(),
                producer=lambda: pipeline,
            )
            == 0
        )
        assert replies == []
        assert list(tmp_path.iterdir()) == []
        assert observed == [] and not pipeline.stopped
        assert '"owner/repo"' in capsys.readouterr().out
    finally:
        pipeline.stop(timeout=2)


def test_failed_authoritative_pr_walk_aborts_before_emission(tmp_path: Path) -> None:
    replies = [("actions/runs?", "[]", 0), ("pulls?", "", 1)]
    observed: list[object] = []
    pipeline = EventPipeline(writer=observed.extend)
    try:
        assert (
            runs_export.main(
                ["--state-dir", str(tmp_path)],
                run_process=_gh_queue(replies),
                timers=ManualTimers(),
                producer=lambda: pipeline,
            )
            == 1
        )
        assert replies == []
        assert not list(tmp_path.iterdir())
        assert observed == [] and not pipeline.stopped
    finally:
        pipeline.stop(timeout=2)


def test_transient_gh_errors_retry_raw_commands_with_existing_budget() -> None:
    replies = [("actions/runs", "HTTP 503", 1), ("actions/runs", "[]", 0)]
    timers = ManualTimers()
    stats = runs_api.FetchStats()
    assert (
        runs_api.fetch_runs(
            "owner/repo",
            datetime(2026, 9, 1, tzinfo=UTC),
            datetime(2026, 9, 3, tzinfo=UTC),
            stats,
            run_process=_gh_queue(replies),
            timers=timers,
            max_pages=1,
        )
        == []
    )
    assert timers.sleeps == [2] and replies == []
    assert stats.api_requests == 2 and stats.retries == 1
