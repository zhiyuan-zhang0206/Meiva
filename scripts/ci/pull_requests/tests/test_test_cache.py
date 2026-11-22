from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.ci.pull_requests import test_cache


def test_is_canary_run_is_deterministic_and_pr_only() -> None:
    assert test_cache.is_canary_run("workflow_dispatch", "1", "1", canary_percent=100) is False
    assert test_cache.is_canary_run("schedule", "1", "1", canary_percent=100) is False
    assert test_cache.is_canary_run("push", "1", "1", canary_percent=100) is False
    assert test_cache.is_canary_run("pull_request", "1", "1", canary_percent=0) is False
    assert test_cache.is_canary_run("pull_request", "1", "1", canary_percent=100) is True

    first = [test_cache.is_canary_run("pull_request", str(i), "1") for i in range(1000)]
    second = [test_cache.is_canary_run("pull_request", str(i), "1") for i in range(1000)]
    assert first == second
    canary_count = sum(first)
    assert 25 <= canary_count <= 80


def test_decide_cache_mode_enforces_all_gates() -> None:
    non_canary_id = next(
        str(i) for i in range(1, 500) if not test_cache.is_canary_run("pull_request", str(i), "1")
    )
    canary_id = next(
        str(i) for i in range(1, 500) if test_cache.is_canary_run("pull_request", str(i), "1")
    )

    assert test_cache.decide_cache_mode(
        event="pull_request",
        backend="true",
        mode="enforce",
        decision="FULL",
        selected_count=0,
        pr_cache_hit="true",
        run_id=non_canary_id,
        run_attempt="1",
    ) == (False, True)

    assert test_cache.decide_cache_mode(
        event="pull_request",
        backend="true",
        mode="enforce",
        decision="SELECTED",
        selected_count=150,
        pr_cache_hit="true",
        run_id=non_canary_id,
        run_attempt="1",
    ) == (False, True)

    assert test_cache.decide_cache_mode(
        event="pull_request",
        backend="true",
        mode="enforce",
        decision="SELECTED",
        selected_count=149,
        pr_cache_hit="true",
        run_id=non_canary_id,
        run_attempt="1",
    ) == (False, False)

    assert test_cache.decide_cache_mode(
        event="pull_request",
        backend="true",
        mode="enforce",
        decision="FULL",
        selected_count=0,
        pr_cache_hit="false",
        run_id=non_canary_id,
        run_attempt="1",
    ) == (False, False)

    assert test_cache.decide_cache_mode(
        event="pull_request",
        backend="true",
        mode="shadow",
        decision="FULL",
        selected_count=0,
        pr_cache_hit="true",
        run_id=non_canary_id,
        run_attempt="1",
    ) == (False, False)

    assert test_cache.decide_cache_mode(
        event="pull_request",
        backend="false",
        mode="enforce",
        decision="FULL",
        selected_count=0,
        pr_cache_hit="true",
        run_id=non_canary_id,
        run_attempt="1",
    ) == (False, False)

    assert test_cache.decide_cache_mode(
        event="workflow_dispatch",
        backend="true",
        mode="enforce",
        decision="FULL",
        selected_count=0,
        pr_cache_hit="true",
        run_id=non_canary_id,
        run_attempt="1",
    ) == (False, False)

    assert test_cache.decide_cache_mode(
        event="pull_request",
        backend="true",
        mode="enforce",
        decision="FULL",
        selected_count=0,
        pr_cache_hit="true",
        run_id=canary_id,
        run_attempt="1",
    ) == (True, False)


def test_decide_cli_writes_github_output_and_handles_empty_selected_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_file = tmp_path / "github_output.txt"
    non_canary_id = next(
        str(i) for i in range(1, 500) if not test_cache.is_canary_run("pull_request", str(i), "1")
    )
    rc = test_cache.main(
        [
            "decide",
            "--event",
            "pull_request",
            "--backend",
            "true",
            "--mode",
            "enforce",
            "--decision",
            "FULL",
            "--selected-count",
            "",
            "--pr-cache-hit",
            "true",
            "--run-id",
            non_canary_id,
            "--run-attempt",
            "1",
            "--github-output",
            str(out_file),
        ]
    )
    assert rc == 0
    assert out_file.read_text(encoding="utf-8") == "canary=false\nskip_enabled=true\n"
    assert "skip_enabled=true" in capsys.readouterr().out


def test_filter_static_tests_preserves_order_and_deduplicates() -> None:
    static = frozenset({"tests/a.py", "tests/c.py"})
    raw = ["tests/b.py", " tests/c.py ", "", "tests/a.py", "tests/c.py"]
    assert test_cache.filter_static_tests(raw, static) == ["tests/c.py", "tests/a.py"]


def test_partition_targets_is_complete_disjoint_and_duration_balanced() -> None:
    targets = [f"tests/test_{i:02d}.py" for i in range(12)]
    durations = {
        "tests/test_00.py": 100.0,
        "tests/test_01.py": 90.0,
        "tests/test_02.py": 80.0,
        "tests/test_03.py": 70.0,
        "tests/test_04.py": 60.0,
        "tests/test_05.py": 50.0,
        "tests/test_06.py": 40.0,
        "tests/test_07.py": 30.0,
        "tests/test_08.py": 20.0,
        "tests/test_09.py": 10.0,
        "tests/test_10.py": 5.0,
        "tests/test_11.py": 5.0,
    }
    buckets = test_cache.partition_targets(targets, durations, groups=4)
    assert len(buckets) == 4
    flattened = [item for bucket in buckets for item in bucket]
    assert len(flattened) == len(targets)
    assert set(flattened) == set(targets)
    loads = [sum(durations[item] for item in bucket) for bucket in buckets]
    assert max(loads) - min(loads) <= 20.0
    assert buckets == test_cache.partition_targets(list(reversed(targets)), durations, groups=4)


def test_partition_targets_handles_fewer_files_than_groups() -> None:
    buckets = test_cache.partition_targets(["tests/a.py", "tests/b.py"], {}, groups=8)
    assert len(buckets) == 8
    non_empty = [bucket for bucket in buckets if bucket]
    assert len(non_empty) == 2
    assert sorted(item for bucket in buckets for item in bucket) == ["tests/a.py", "tests/b.py"]
    assert test_cache.partition_targets([], {}, groups=8) == [[] for _ in range(8)]


def test_shard_targets_cli_filters_to_static_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FakeLanes:
        static = frozenset({"tests/s1.py", "tests/s2.py"})

    def _fake_lanes(_root: Path) -> _FakeLanes:
        return _FakeLanes()

    monkeypatch.setattr(test_cache, "lanes_at", _fake_lanes)
    plan_path = tmp_path / "plan.json"
    _ = plan_path.write_text(
        json.dumps(
            {
                "keys": {"tests/s1.py": "k1", "tests/s2.py": "k2"},
                "skippable": ["tests/s0.py"],
                "run": ["tests/s1.py", "tests/native.py", "tests/s2.py"],
                "uncacheable": {},
                "unbounded": {},
            }
        ),
        encoding="utf-8",
    )
    dur_path = tmp_path / "durations.json"
    _ = dur_path.write_text(json.dumps({"tests/s1.py": 10.0, "tests/s2.py": 5.0}), encoding="utf-8")

    collected: list[str] = []
    for group in (1, 2, 3):
        out_path = tmp_path / f"shard-{group}.txt"
        rc = test_cache.main(
            [
                "shard-targets",
                "--plan",
                str(plan_path),
                "--durations",
                str(dur_path),
                "--group",
                str(group),
                "--groups",
                "3",
                "--out",
                str(out_path),
            ]
        )
        assert rc == 0
        lines = [line for line in out_path.read_text(encoding="utf-8").splitlines() if line]
        collected.extend(lines)
    assert sorted(collected) == ["tests/s1.py", "tests/s2.py"]
    assert (tmp_path / "shard-3.txt").read_text(encoding="utf-8") == ""


def test_format_summary_for_active_skip_and_shadow_modes() -> None:
    plan: dict[str, object] = {
        "skippable": ["tests/a.py", "tests/b.py"],
        "run": ["tests/c.py"],
        "uncacheable": {"tests/u.py": "flaky"},
        "unbounded": {"tests/t.py": "tree-scan"},
    }
    durations = {"tests/a.py": 12.4, "tests/b.py": 7.6}
    active = test_cache.format_summary(
        plan=plan,
        report=None,
        durations=durations,
        skip_enabled=True,
        canary=False,
        recorded=1,
        label="active skip",
    )
    assert "### Test result cache (active skip)" in active
    assert "- skipped: 2" in active
    assert "- ran: 1" in active
    assert "- seconds saved (by `.test_durations`): 20" in active
    assert "- recorded: 1" in active

    active_with_zero_report = test_cache.format_summary(
        plan=plan,
        report={
            "would_skip": 2,
            "would_skip_passed": 0,
            "would_skip_failed": [],
            "would_skip_not_run": ["tests/a.py", "tests/b.py"],
            "saved_seconds": 0.0,
        },
        durations=durations,
        skip_enabled=True,
        canary=False,
        recorded=1,
        label="active skip",
    )
    assert "- seconds saved (by `.test_durations`): 20" in active_with_zero_report

    report: dict[str, object] = {
        "would_skip": 2,
        "would_skip_passed": 2,
        "would_skip_failed": [],
        "would_skip_not_run": [],
        "saved_seconds": 20.0,
        "uncacheable": 1,
        "unbounded": 1,
    }
    canary = test_cache.format_summary(
        plan=plan,
        report=report,
        durations=durations,
        skip_enabled=False,
        canary=True,
        recorded=3,
        label="canary",
    )
    assert "### Test result cache (canary)" in canary
    assert "- would skip: 2 (2 passed when run)" in canary
    assert "- would skip but failed: 0 []" in canary
    assert "- recorded: 3" in canary
