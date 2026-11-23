from __future__ import annotations

import hashlib
from pathlib import Path

from scripts.ci.pull_requests import test_cache


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


def test_static_shards_plan_every_static_target_once() -> None:
    static = frozenset(f"tests/test_{i}.py" for i in range(12))
    selected = [*sorted(static), "tests/test_native.py", "tests/test_0.py"]
    node_seconds = {f"{path}::test_a": 200.0 for path in static}
    node_seconds["tests/test_0.py::test_b"] = 40.0
    node_seconds["tests/test_native.py::test_a"] = 30000.0

    shards = test_cache.static_shards(selected, static, node_seconds)

    assert 1 < len(shards) <= 8
    assert all(shards)
    assert sorted(path for shard in shards for path in shard) == sorted(static)
    assert shards == test_cache.static_shards(list(reversed(selected)), static, node_seconds)


def test_static_shards_small_empty_and_cold_cache() -> None:
    static = frozenset(f"tests/test_{i}.py" for i in range(1000))
    assert test_cache.static_shards(["tests/test_native.py"], static, {}) == [[]]
    assert test_cache.static_shards(["tests/test_1.py"], static, {}) == [["tests/test_1.py"]]
    shards = test_cache.static_shards(sorted(static), static, {})
    assert len(shards) == 8
    assert sorted(path for shard in shards for path in shard) == sorted(static)


def test_static_plan_pins_each_listing_and_only_allows_an_explicit_empty_lane(
    tmp_path: Path,
) -> None:
    shards = [["tests/test_a.py", "tests/test_b.py"], ["tests/test_c.py"]]
    matrix = test_cache.publish_static_plan(tmp_path, shards)
    assert [row["group"] for row in matrix] == [1, 2]
    for row, paths in zip(matrix, shards, strict=True):
        listing = (tmp_path / f"static-{row['group']}.txt").read_bytes()
        assert listing.decode().splitlines() == paths
        assert row["digest"] == hashlib.sha256(listing).hexdigest()
        assert row["min_tests"] == "1"

    [empty] = test_cache.publish_static_plan(tmp_path, [[]])
    assert empty["min_tests"] == "0"
    assert (tmp_path / "static-1.txt").read_bytes() == b""


def test_format_summary_checks_cached_passes_against_executed_results() -> None:
    plan: dict[str, object] = {
        "skippable": ["tests/a.py", "tests/b.py"],
        "uncacheable": {"tests/u.py": "flaky"},
        "unbounded": {"tests/t.py": "tree-scan"},
    }
    report: dict[str, object] = {
        "would_skip": 2,
        "would_skip_passed": 1,
        "would_skip_failed": ["tests/b.py"],
        "would_skip_not_run": [],
        "saved_seconds": 20.0,
    }
    summary = test_cache.format_summary(plan=plan, report=report, recorded=3, label="shadow")
    assert "### Test result cache (shadow)" in summary
    assert "- would skip: 2 (1 passed when run)" in summary
    assert "- would skip but failed: 1 ['tests/b.py']" in summary
    assert "- recorded: 3" in summary
