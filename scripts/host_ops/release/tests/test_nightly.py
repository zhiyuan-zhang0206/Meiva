from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

import pytest

from scripts.host_ops.release import nightly

CLEAN = """<?xml version="1.0"?>
<testsuites>
  <testsuite name="pytest" tests="40" failures="0" errors="0" skipped="2">
    <testcase name="a" />
  </testsuite>
</testsuites>
"""

RED = """<?xml version="1.0"?>
<testsuite name="pytest" tests="40" failures="1" errors="0" skipped="0">
  <testcase name="a"><failure message="boom" /></testcase>
</testsuite>
"""


def _write(directory: Path, name: str, body: str) -> Path:
    path = directory / f"{name}.xml"
    path.write_text(body, encoding="utf-8")
    return path


def test_a_clean_shard_counts_tests_and_skips(tmp_path: Path) -> None:
    result = nightly.read_shard(_write(tmp_path, "backend-1", CLEAN))

    assert (result.tests, result.failures, result.errors, result.skipped) == (40, 0, 0, 2)
    assert result.ok


def test_a_failure_makes_the_shard_red(tmp_path: Path) -> None:
    assert not nightly.read_shard(_write(tmp_path, "backend-2", RED)).ok


def test_all_expected_shards_green_is_the_only_green(tmp_path: Path) -> None:
    results = [nightly.read_shard(_write(tmp_path, f"backend-{group}", CLEAN)) for group in (1, 2)]

    verdict = nightly.judge(results, {"backend": 2})

    assert verdict.ok
    assert verdict.reason == "all-shards-green"


def test_a_missing_shard_is_red_even_when_every_reported_shard_passed(tmp_path: Path) -> None:
    results = [nightly.read_shard(_write(tmp_path, "backend-1", CLEAN))]

    verdict = nightly.judge(results, {"backend": 2})

    assert not verdict.ok
    assert verdict.reason.startswith("missing-shards")


def test_one_red_shard_names_itself(tmp_path: Path) -> None:
    results = [
        nightly.read_shard(_write(tmp_path, "backend-1", CLEAN)),
        nightly.read_shard(_write(tmp_path, "backend-2", RED)),
    ]

    verdict = nightly.judge(results, {"backend": 2})

    assert not verdict.ok
    assert verdict.reason == "failed-shards: backend-2"


def test_a_truncated_junit_file_fails_loudly(tmp_path: Path) -> None:
    path = _write(tmp_path, "backend-1", "<testsuites><testsuite")

    with pytest.raises(ElementTree.ParseError):
        nightly.read_shard(path)


def test_an_empty_suite_is_not_a_pass(tmp_path: Path) -> None:
    result = nightly.read_shard(
        _write(
            tmp_path,
            "backend-3",
            '<?xml version="1.0"?><testsuite tests="0" failures="0" errors="0"></testsuite>',
        )
    )

    assert not result.ok


def test_the_cli_writes_the_record_and_exits_nonzero_when_red(tmp_path: Path) -> None:
    junit = tmp_path / "junit"
    junit.mkdir()
    _write(junit, "backend-1", CLEAN)
    _write(junit, "backend-2", RED)
    output = tmp_path / "nightly" / "sha.json"

    code = nightly.main(
        [
            "--junit-dir",
            str(junit),
            "--sha",
            "deadbeef",
            "--backend-shards",
            "2",
            "--e2e-shards",
            "0",
            "--output",
            str(output),
        ]
    )

    assert code == 1
    assert '"ok": false' in output.read_text(encoding="utf-8")


def test_the_cli_exits_zero_when_every_shard_is_green(tmp_path: Path) -> None:
    junit = tmp_path / "junit"
    junit.mkdir()
    _write(junit, "backend-1", CLEAN)
    output = tmp_path / "nightly" / "sha.json"

    code = nightly.main(
        [
            "--junit-dir",
            str(junit),
            "--sha",
            "deadbeef",
            "--backend-shards",
            "1",
            "--e2e-shards",
            "0",
            "--output",
            str(output),
        ]
    )

    assert code == 0
    assert '"ok": true' in output.read_text(encoding="utf-8")
