from __future__ import annotations

from pathlib import Path

import base.paths
from base.deploy.lifecycle import launch_failures
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records


def _report_path(home: Path) -> Path:
    return base.paths.launch_failures_path(home)


def test_record_then_take_round_trips_and_clears(
    tmp_path: Path, loguru_records: LogRecords
) -> None:
    launch_failures.record(tmp_path, ["meiva-gateway", "meiva-browser-mcp"])

    assert launch_failures.take(tmp_path) == ["meiva-gateway", "meiva-browser-mcp"]
    assert not _report_path(tmp_path).exists(), "take consumes the report exactly once"
    assert launch_failures.take(tmp_path) == []
    assert loguru_records.events("launch_failures") == [], "a clean read reports nothing"


def test_a_malformed_report_is_reported_not_read_as_no_failures(
    tmp_path: Path, loguru_records: LogRecords
) -> None:
    path = _report_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[meiva-gateway", encoding="utf-8")

    assert launch_failures.take(tmp_path) == []

    [event] = loguru_records.events("launch_failures")
    assert event["level"].name == "WARNING"
    assert str(path) in event["message"]
    assert "not reported" in event["message"]
