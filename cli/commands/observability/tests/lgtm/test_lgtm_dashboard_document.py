from __future__ import annotations

from pathlib import Path

from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from cli.commands.observability import _lgtm_provisioning as prov


def test_unreadable_dashboard_is_reported_and_skipped(
    tmp_path: Path, loguru_records: LogRecords
) -> None:
    dashboard = tmp_path / "board.json"
    dashboard.write_text("{not json", encoding="utf-8")

    assert prov.dashboard_document(dashboard) == {}

    [event] = loguru_records.events("lgtm_dashboard")
    assert str(dashboard) in event["message"]
    assert "not provisioned" in event["message"]


def test_non_object_dashboard_is_reported_and_skipped(
    tmp_path: Path, loguru_records: LogRecords
) -> None:
    dashboard = tmp_path / "board.json"
    dashboard.write_text("[1, 2]", encoding="utf-8")

    assert prov.dashboard_document(dashboard) == {}

    [event] = loguru_records.events("lgtm_dashboard")
    assert "not a JSON object" in event["message"]


def test_a_readable_dashboard_is_not_reported(tmp_path: Path, loguru_records: LogRecords) -> None:
    dashboard = tmp_path / "board.json"
    dashboard.write_text('{"uid": "abc"}', encoding="utf-8")

    assert prov.dashboard_document(dashboard) == {"uid": "abc"}
    assert loguru_records.events("lgtm_dashboard") == []
