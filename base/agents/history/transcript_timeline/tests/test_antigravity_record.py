from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from base.agents.history.transcript_timeline.record.harness import reply_key
from base.agents.history.transcript_timeline.record.match import assign_inputs
from base.agents.history.transcript_timeline.sent import SentRow
from base.agents.history.transcript_timeline.tests.support import window, write
from base.lm.harness.antigravity.transcript.window import EntryParser, input_wire_ids
from base.lm.harness.session.transcript.entries import (
    Failure,
    InputRow,
    Notice,
    ToolCall,
    ToolResult,
    ToolStatus,
)
from base.lm.harness.session.transcript.record_file import FileRow
from base.log import logger

STAMP = "2026-11-13T08:00:00Z"


def row(kind: str, **extra: object) -> dict[str, object]:
    return {"type": kind, "status": "DONE", "step_index": 1, "created_at": STAMP, **extra}


def rows() -> list[dict[str, object]]:
    return [
        row(
            "PLANNER_RESPONSE",
            thinking="reason",
            content="answer",
            tool_calls=[
                {"name": "run_command", "args": {"CommandLine": "echo one"}},
                {"name": "read_file", "args": {"Path": "own.txt"}},
            ],
        ),
        row("GENERIC", content="one"),
        row("GENERIC", content="two"),
        row("ERROR_MESSAGE", error="synthetic failure"),
        row("CHECKPOINT", content="hidden summary"),
    ]


def test_offsets_pairing_and_append_reads_agree_with_duplicate_steps() -> None:
    source = [FileRow(i * 100, (i + 1) * 100, r) for i, r in enumerate(rows())]
    whole = EntryParser().entries(source)
    live = EntryParser()
    assert whole == tuple(e for r in source for e in live.entries([r]))
    calls = [e for e in whole if isinstance(e, ToolCall)]
    results = [e for e in whole if isinstance(e, ToolResult)]
    assert [e.call_id for e in results] == [e.call_id for e in calls]
    assert len({e.key for e in whole}) == len(whole)
    assert any(isinstance(e, Failure) for e in whole)


@pytest.mark.parametrize("size", [1, 2, 3, 50])
def test_pages_equal_full_projection_without_splitting_offset(size: int, tmp_path: Path) -> None:
    text = "".join(json.dumps(r) + "\n" for r in rows())
    file = write(tmp_path / "record.jsonl", text)
    full = window(file, harness="antigravity").timeline_items
    actual = []
    before = None
    for _ in range(20):
        page = window(file, harness="antigravity", limit=size, before=before)
        assert page.items or not page.has_more
        actual = page.timeline_items + actual
        if not page.has_more:
            break
        before = page.items[0].offset
    else:
        pytest.fail("pagination did not terminate")
    assert actual == full
    assert all(not item.activity_pending for item in actual)
    assert all(
        item.payload.startswith(("status:", "error:"))
        for item in actual
        if item.kind == "system_marker"
    )


def test_notices_do_not_break_continuation_and_invalid_is_an_error() -> None:
    records = [
        row("PLANNER_RESPONSE", tool_calls=[{"name": "run_command", "args": {"synthetic": True}}]),
        row("GENERIC", status="RUNNING", content="background"),
        row(
            "SYSTEM_MESSAGE",
            content="The following is a <SYSTEM_MESSAGE> from the CLI.\n\n<SYSTEM_MESSAGE>\n[Message] timestamp=t sender=c/task-1 priority=normal content=done\n</SYSTEM_MESSAGE>",
        ),
        row("GENERIC", status="INVALID", content="failed", error="bad argument"),
    ]
    entries = EntryParser().entries([FileRow(i, i + 1, r) for i, r in enumerate(records)])
    result = [e for e in entries if isinstance(e, ToolResult)]
    assert len(result) == 2
    assert result[0].call_id == result[1].call_id
    assert result[1].status is ToolStatus.ERROR
    assert "bad argument" in result[1].content
    assert any(isinstance(e, Notice) for e in entries)


def test_matching_uses_content_claim_window_and_first_file_position() -> None:
    stamp = datetime(2026, 11, 13, tzinfo=UTC)
    inbox = [
        SentRow(2, 1, "chat", "user", "same", None, stamp, stamp),
        SentRow(1, 1, "chat", "user", "same", None, stamp, stamp),
        SentRow(3, 1, "chat", "user", "other", None, stamp, stamp),
        SentRow(4, 1, "chat", "user", "same", None, stamp, stamp - timedelta(hours=1)),
    ]
    requests = {
        "agy:100": InputRow(stamp, "envelope same other"),
        "agy:200": InputRow(stamp, "same"),
    }
    assert {k: [r.id for r in v] for k, v in assign_inputs(inbox, requests).items()} == {
        "agy:100": [1, 2, 3]
    }


def test_publications_are_conversation_scoped_and_require_identity() -> None:
    assert reply_key("antigravity", "agy:100", "one") == "harness:antigravity:one:100"
    assert reply_key("antigravity", "agy:100", "two") != reply_key("antigravity", "agy:100", "one")
    with pytest.raises(ValueError, match="conversation"):
        reply_key("antigravity", "agy:100")
    assert reply_key("codex", "agent:original") == "harness:codex:original"


def test_a_long_background_turn_keeps_reply_visibility_across_pages(tmp_path: Path) -> None:
    notice = row(
        "SYSTEM_MESSAGE",
        content="<SYSTEM_MESSAGE>\n[Message] timestamp=t sender=c/task-1 priority=normal content=timer\n</SYSTEM_MESSAGE>",
    )
    records = [notice]
    for _ in range(120):
        records.extend(
            [
                row(
                    "PLANNER_RESPONSE",
                    tool_calls=[{"name": "run_command", "args": {"CommandLine": "echo synthetic"}}],
                ),
                row("GENERIC", content="X" * 20000),
            ]
        )
    records.append(row("PLANNER_RESPONSE", content="Background result", tool_calls=[]))
    file = write(tmp_path / "long-background.jsonl", "".join(json.dumps(r) + "\n" for r in records))
    full = window(file, harness="antigravity").timeline_items
    page = window(file, harness="antigravity", limit=1).timeline_items
    assert page[-1] == full[-1]
    assert page[-1].user_facing is False


def test_invalid_input_and_notice_report_shape_without_logging_body() -> None:
    bad = [
        FileRow(0, 1, row("USER_INPUT", content=7)),
        FileRow(1, 2, row("SYSTEM_MESSAGE", content="DO_NOT_LOG_SYNTHETIC_BODY")),
    ]
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        assert EntryParser().entries(bad) == ()
    finally:
        logger.remove(sink)
    assert input_wire_ids(bad) == {}
    assert len(messages) == 2
    assert "DO_NOT_LOG_SYNTHETIC_BODY" not in "".join(messages)
