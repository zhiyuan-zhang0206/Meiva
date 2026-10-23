"""PR-flow exporter — sampling, aggregation, cache, and failure-path contracts."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from base.host.net.tests.fake_url_opener import Answer, FakeUrlOpener
from base.telemetry import Event, EventPipeline
from scripts.ci.pull_requests import pr_flow_export as pr_flow

_TZ = ZoneInfo("Asia/Shanghai")


class GhReplies:
    def __init__(self, payloads: list[object], *, code: int = 0) -> None:
        self.payloads = payloads
        self.code = code
        self.commands: list[list[str]] = []

    def __call__(
        self, argv: Sequence[str], *, timeout: float, capture_output: bool, text: bool
    ) -> subprocess.CompletedProcess[str]:
        assert timeout == 60 and capture_output and text
        assert list(argv[:2]) == ["gh", "api"]
        self.commands.append(list(argv))
        assert self.payloads, f"unmatched: {argv}"
        return subprocess.CompletedProcess(
            argv, self.code, json.dumps(self.payloads.pop(0)), "gh down" if self.code else ""
        )


def _meta(
    number: int,
    *,
    created: str = "2026-09-12T02:00:00Z",
    merged: str = "2026-09-12T03:00:00Z",
    updated: str | None = None,
) -> dict[str, Any]:
    return {
        "number": number,
        "created_at": created,
        "updated_at": updated or merged,
        "merged_at": merged,
    }


# ── window + percentile math ────────────────────────────────────────────────


def test_window_days_are_the_complete_days_before_today() -> None:
    now = datetime(2026, 9, 14, 0, 25, tzinfo=UTC)  # 08:25 CST
    days = pr_flow.window_days(now, 3, _TZ)
    assert days == [date(2026, 9, 11), date(2026, 9, 12), date(2026, 9, 13)]


def test_percentile_linear_interpolation() -> None:
    values = [10.0, 20.0, 30.0, 40.0]
    assert pr_flow.percentile(values, 0.5) == 25.0
    assert pr_flow.percentile(values, 0.9) == pytest.approx(37.0)
    assert pr_flow.percentile([7.0], 0.9) == 7.0


# ── record building ─────────────────────────────────────────────────────────


def test_build_record_prefers_ready_for_review_over_created_at() -> None:
    events = [{"event": "ready_for_review", "created_at": "2026-09-12T02:10:00Z"}]
    record = pr_flow.build_record(_meta(1), events)
    assert record.ready_at == "2026-09-12T02:10:00Z"
    fallback = pr_flow.build_record(_meta(2), [])
    assert fallback.ready_at == "2026-09-12T02:00:00Z"


# ── daily aggregation ───────────────────────────────────────────────────────


def _record(number: int, *, merged: str, ready: str):
    return pr_flow.PrRecord(
        number=number,
        updated_at=merged,
        created_at=ready,
        merged_at=merged,
        ready_at=ready,
    )


def test_compute_days_groups_by_merge_day_and_omits_empty_samples() -> None:
    records = [
        _record(1, merged="2026-09-12T02:00:00Z", ready="2026-09-12T01:00:00Z"),  # 3600s
        _record(2, merged="2026-09-12T03:00:00Z", ready="2026-09-12T01:00:00Z"),  # 7200s
        _record(3, merged="2026-09-13T03:00:00Z", ready="2026-09-13T02:30:00Z"),  # 1800s
    ]
    days = [date(2026, 9, 11), date(2026, 9, 12), date(2026, 9, 13)]
    payload = pr_flow.compute_days(records, days, _TZ, flake_counts=None)
    d12 = payload["2026-09-12"]
    assert d12["merged_count"] == 2
    assert d12["ready_to_merge_median_seconds"] == pytest.approx(5400.0)
    assert d12["ready_to_merge_p90_seconds"] == pytest.approx(6840.0)
    assert "qa_rounds_mean" not in d12
    assert "qa_rereview_share" not in d12
    assert "flake_new_quarantines" not in d12  # unreachable flake source is not zero
    d11 = payload["2026-09-11"]
    assert d11 == {"merged_count": 0}
    assert "flake_new_quarantines" not in payload["2026-09-13"]
    with_flakes = pr_flow.compute_days(records, days, _TZ, flake_counts={date(2026, 9, 13): 3})
    assert with_flakes["2026-09-13"]["flake_new_quarantines"] == 3
    assert with_flakes["2026-09-12"]["flake_new_quarantines"] == 0


def test_duration_skips_non_positive_spans() -> None:
    record = _record(9, merged="2026-09-12T01:00:00Z", ready="2026-09-12T02:00:00Z")
    assert pr_flow.duration_seconds(record) is None


# ── cache-based record collection ───────────────────────────────────────────


def test_collect_records_reuses_cache_only_when_updated_at_is_unchanged() -> None:
    days = [date(2026, 9, 12)]
    meta = _meta(7)
    cached = {
        "7": {
            "updated_at": meta["updated_at"],
            "created_at": meta["created_at"],
            "merged_at": meta["merged_at"],
            "ready_at": meta["created_at"],
            "partial": False,
        }
    }
    stats = pr_flow.RunStats()
    records = pr_flow.collect_records(
        [meta], days, _TZ, "r/x", cached, stats, run_process=GhReplies([])
    )
    assert stats.timeline_cache_hits == 1
    assert stats.timeline_fetches == 0
    assert records[0].ready_at == meta["created_at"]

    moved = _meta(7, updated="2026-09-12T09:00:00Z")
    fetched = GhReplies([[]])

    stats2 = pr_flow.RunStats()
    records2 = pr_flow.collect_records(
        [moved], days, _TZ, "r/x", cached, stats2, run_process=fetched
    )
    assert len(fetched.commands) == 1
    assert "/issues/7/timeline?" in fetched.commands[0][2]
    assert stats2.timeline_fetches == 1
    assert records2[0].updated_at == "2026-09-12T09:00:00Z"


def test_collect_records_falls_back_to_cache_then_partial_on_fetch_failure() -> None:
    days = [date(2026, 9, 12)]
    meta = _meta(8, updated="2026-09-12T09:00:00Z")
    cached = {
        "8": {
            "updated_at": "2026-09-12T03:00:00Z",
            "created_at": meta["created_at"],
            "merged_at": meta["merged_at"],
            "ready_at": meta["created_at"],
            "partial": False,
        }
    }

    boom = GhReplies([None, None], code=1)

    stats = pr_flow.RunStats()
    records = pr_flow.collect_records([meta], days, _TZ, "r/x", cached, stats, run_process=boom)
    assert stats.timeline_failures == 1
    assert records[0].ready_at == meta["created_at"]  # stale cached record kept
    assert records[0].partial is False

    records2 = pr_flow.collect_records([meta], days, _TZ, "r/x", {}, stats, run_process=boom)
    assert records2[0].partial is True
    assert records2[0].ready_at == ""


# ── fetchers: pagination, stops, failure paths ──────────────────────────────


def test_fetch_closed_prs_stops_when_a_page_tail_predates_the_window() -> None:
    since = datetime(2026, 8, 15, tzinfo=UTC)  # time-bomb-ok: explicit fixture window input
    recent = [_meta(n, updated="2026-09-12T03:00:00Z") for n in range(100, 200)]
    tail_old = [_meta(n, updated="2026-09-12T03:00:00Z") for n in range(99)]
    tail_old.append(_meta(1, updated="2026-08-01T03:00:00Z"))
    replies = GhReplies([recent, tail_old, "MUST NOT FETCH"])
    stats = pr_flow.RunStats()
    prs = pr_flow.fetch_closed_prs(
        "r/x", since, stats, run_process=replies, max_pages=pr_flow.MAX_LIST_PAGES
    )
    assert [int(argv[2].rsplit("page=", 1)[1]) for argv in replies.commands] == [1, 2]
    assert len(prs) == 200
    assert replies.payloads == ["MUST NOT FETCH"]


def test_fetch_closed_prs_returns_on_a_short_page() -> None:
    replies = GhReplies([[_meta(2, updated="2026-09-12T03:00:00Z")]])
    prs = pr_flow.fetch_closed_prs(
        "r/x",
        datetime(2026, 8, 15, tzinfo=UTC),
        pr_flow.RunStats(),
        run_process=replies,
        max_pages=pr_flow.MAX_LIST_PAGES,
    )
    assert [int(argv[2].rsplit("page=", 1)[1]) for argv in replies.commands] == [1]
    assert len(prs) == 1


def test_fetch_closed_prs_raises_when_the_walk_exceeds_its_budget() -> None:
    page = [_meta(n, updated="2026-09-12T03:00:00Z") for n in range(100)]
    replies = GhReplies([page, page])
    with pytest.raises(pr_flow.PrFlowError, match="budget"):
        pr_flow.fetch_closed_prs(
            "r/x",
            datetime(2026, 8, 15, tzinfo=UTC),
            pr_flow.RunStats(),
            run_process=replies,
            max_pages=2,
        )
    assert len(replies.commands) == 2 and replies.payloads == []


def test_fetch_timeline_bounds_pagination() -> None:
    page = [{"event": "committed", "sha": "a" * 40, "commit_date": "2026-09-12T02:00:00Z"}] * 100
    replies = GhReplies([page] * pr_flow.MAX_TIMELINE_PAGES)
    stats = pr_flow.RunStats()
    events = pr_flow.fetch_timeline(
        "r/x", 5, stats, run_process=replies, max_pages=pr_flow.MAX_TIMELINE_PAGES
    )
    assert len(events) == pr_flow.MAX_TIMELINE_PAGES * 100
    assert stats.gh_api_calls == pr_flow.MAX_TIMELINE_PAGES
    assert replies.payloads == []


def test_fetch_queue_depth_returns_none_on_trunk_error() -> None:
    opener = FakeUrlOpener()
    opener.on("https://api.trunk.io/v1/getQueue", Answer(status=503))
    stats = pr_flow.RunStats()
    assert pr_flow.fetch_queue_depth("r/x", "tok", stats, open_url=opener) is None
    assert stats.trunk_error == "HTTP 503"


def test_fetch_quarantined_walks_next_page_token() -> None:
    opener = FakeUrlOpener()
    opener.on(
        "https://api.trunk.io/v1/flaky-tests/list-quarantined-tests",
        Answer(
            json.dumps(
                {
                    "quarantined_tests": [
                        {"test_case_id": "a", "quarantined_at": "2026-09-12T01:00:00Z"}
                    ],
                    "page": {"next_page_token": "tok-2"},
                }
            )
        ),
        Answer(json.dumps({"quarantined_tests": [{"test_case_id": "b"}], "page": {}})),
    )
    stats = pr_flow.RunStats()
    result = pr_flow.fetch_quarantined("r/x", "tok", stats, open_url=opener)
    assert result is not None and len(result) == 2
    calls = [json.loads(call.data or b"{}") for call in opener.calls]
    assert calls[1]["page_query"] == {"page_size": 100, "page_token": "tok-2"}


def test_count_quarantined_by_day_converts_to_cluster_timezone() -> None:
    # 2026-09-09T18:00:00Z is 2026-09-10 02:00 in Asia/Shanghai.
    quarantined = [
        {"quarantined_at": "2026-09-09T18:00:00Z"},
        {"quarantined_at": "2026-09-10T01:00:00Z"},  # 09:00 CST, still 09-10
        {"quarantined_at": "bogus"},
        {"no_stamp": True},
    ]
    days = [date(2026, 9, 10)]
    counts = pr_flow.count_quarantined_by_day(quarantined, days, _TZ)
    assert counts == {date(2026, 9, 10): 2}


# ── snapshot persistence ────────────────────────────────────────────────────


def test_save_json_is_atomic_and_load_cache_heals_corruption(tmp_path: Path) -> None:
    target = tmp_path / "state" / "cache.json"
    pr_flow.save_json(target, {"version": 1, "prs": {"1": {"x": 1}}})
    assert pr_flow.load_cache(target)["1"]["x"] == 1

    target.write_text("{not json", encoding="utf-8")
    assert pr_flow.load_cache(target) == {}
    assert not list(target.parent.glob("*.tmp"))


def test_save_json_failed_replace_keeps_the_prior_file_and_no_tmp(tmp_path: Path) -> None:
    """A failed rename must leave the target intact and must not
    strand the temporary beside it -- the atomicity the write-and-rename
    actually buys (QA fold-in from the #2510 review)."""
    target = tmp_path / "state" / "cache.json"
    target.mkdir(parents=True)
    with pytest.raises(IsADirectoryError):
        pr_flow.save_json(target, {"version": 2, "prs": {"1": {"x": 1}}})

    assert target.is_dir()
    assert list(target.parent.glob("*.tmp")) == []


def test_emit_snapshot_skips_the_pipeline_in_dry_run() -> None:
    emitted: list[Event] = []
    writer = EventPipeline(writer=emitted.extend)
    snapshot: dict[str, Any] = {"days": {}, "run": {}}
    try:
        pr_flow.emit_snapshot(snapshot, dry_run=True, pipeline=writer)
        assert emitted == []
        pr_flow.emit_snapshot(snapshot, dry_run=False, pipeline=writer)
        assert [event.event_name for event in emitted] == ["pr_flow_run"]
        assert all(event.process == pr_flow.PROCESS_NAME for event in emitted)
    finally:
        writer.stop(timeout=2)
