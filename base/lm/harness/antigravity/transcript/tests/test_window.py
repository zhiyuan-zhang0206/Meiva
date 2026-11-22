from __future__ import annotations

import pytest

from base.lm.harness.antigravity.transcript.window import EntryParser
from base.lm.harness.session.transcript.entries import AgentText
from base.lm.harness.session.transcript.record_file import FileRow, UnreadRows
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records

_STAMP = "2026-11-22T06:00:00Z"


@pytest.mark.parametrize(
    "status",
    ["GENERATING", "RUNNING", "CANCELED", "CANCELLED", "ERROR", "FAILED"],
)
def test_planner_response_known_non_done_statuses_skip_without_unread_warning(
    status: str, loguru_records: LogRecords
) -> None:
    unread = UnreadRows()
    row = FileRow(
        offset=0,
        end=100,
        row={"type": "PLANNER_RESPONSE", "status": status, "created_at": _STAMP, "content": ""},
    )

    entries = EntryParser(unread).entries([row])

    assert entries == ()
    assert not any(
        record["extra"].get("event") == "harness_record_row_unread" for record in loguru_records
    )


def test_planner_response_unknown_status_still_reports_unread_anomaly(
    loguru_records: LogRecords,
) -> None:
    unread = UnreadRows()
    row = FileRow(
        offset=0,
        end=100,
        row={"type": "PLANNER_RESPONSE", "status": "MYSTERY_STATE", "created_at": _STAMP},
    )

    entries = EntryParser(unread).entries([row])

    assert entries == ()
    unread_events = [
        record["extra"]
        for record in loguru_records
        if record["extra"].get("event") == "harness_record_row_unread"
    ]
    assert len(unread_events) == 1
    assert unread_events[0]["detail"] == "PLANNER_RESPONSE.status"


def test_planner_response_done_still_emits_agent_text() -> None:
    unread = UnreadRows()
    row = FileRow(
        offset=0,
        end=100,
        row={
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": _STAMP,
            "content": "hello",
        },
    )

    entries = EntryParser(unread).entries([row])

    assert len(entries) == 1
    assert isinstance(entries[0], AgentText)
    assert entries[0].text == "hello"
