from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from base.agents.history.transcript_timeline.record.harness import reply_key
from base.agents.history.transcript_timeline.tests.support import window, write
from base.lm.harness.antigravity.transcript.file import AntigravityRecordFile
from base.lm.harness.antigravity.transcript.input_store import (
    inputs_path,
    record_sent_input,
    text_sha256,
)
from base.lm.harness.antigravity.transcript.window import EntryParser, input_wire_ids, wire_id
from base.lm.harness.session.transcript.entries import (
    Failure,
    HumanInput,
    InputRow,
    Notice,
    ToolCall,
    ToolResult,
    ToolStatus,
)
from base.lm.harness.session.transcript.record_file import (
    BLOCK_BYTES,
    FileRow,
    RecordReadBudgetError,
    UnreadRows,
)
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
    whole = EntryParser(UnreadRows()).entries(source)
    live = EntryParser(UnreadRows())
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
    entries = EntryParser(UnreadRows()).entries(
        [FileRow(i, i + 1, r) for i, r in enumerate(records)]
    )
    result = [e for e in entries if isinstance(e, ToolResult)]
    assert len(result) == 2
    assert result[0].call_id == result[1].call_id
    assert result[1].status is ToolStatus.ERROR
    assert "bad argument" in result[1].content
    assert any(isinstance(e, Notice) for e in entries)


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
        assert EntryParser(UnreadRows()).entries(bad) == ()
    finally:
        logger.remove(sink)
    assert input_wire_ids(bad) == {}
    assert len(messages) == 2
    assert "DO_NOT_LOG_SYNTHETIC_BODY" not in "".join(messages)


def test_cli_reminder_system_message_is_ignored_without_unread_warning() -> None:
    reminder = (
        "The following is a <SYSTEM_MESSAGE> not actually sent by the user. It is provided by the "
        "system as important information to pay attention to.\n\n"
        "<SYSTEM_MESSAGE>\n"
        "Reminder: work efficiently. In this turn, issue in parallel every independent search, "
        "read and edit or write you need next.\n"
        "</SYSTEM_MESSAGE>"
    )
    records = [FileRow(0, 1, row("SYSTEM_MESSAGE", content=reminder))]
    warnings: list[str] = []
    sink = logger.add(lambda message: warnings.append(str(message)), level="WARNING")
    try:
        assert EntryParser(UnreadRows()).entries(records) == ()
    finally:
        logger.remove(sink)
    assert warnings == []


def legacy_marker(key: str) -> str:
    return f"<!-- meiva-input:{hashlib.sha256(key.encode()).hexdigest()} -->"


@pytest.mark.parametrize(
    "body",
    [
        "before <USER_REQUEST>literal opening after",
        "before <USER_REQUEST>nested</USER_REQUEST> after",
        "before </USER_REQUEST>literal closing after",
    ],
)
def test_literal_request_tags_keep_the_whole_text_and_the_bound_key_names_the_input(
    body: str,
) -> None:
    stamp = datetime.fromisoformat(STAMP.replace("Z", "+00:00"))
    content = (
        f"<USER_REQUEST>\n{body}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>time</ADDITIONAL_METADATA>"
    )
    records = [FileRow(100, 200, row("USER_INPUT", content=content, meiva_input_key="inbound-1"))]
    wire = wire_id("inbound-1")
    assert input_wire_ids(records) == {wire: InputRow(stamp)}
    whole = EntryParser(UnreadRows()).entries(records)
    live = EntryParser(UnreadRows())
    assert whole == tuple(entry for record in records for entry in live.entries([record]))
    assert len(whole) == 1 and isinstance(whole[0], HumanInput)
    assert whole[0].text == body
    assert whole[0].wire_id == wire


def test_a_row_no_ledger_binds_is_never_wanted_matched_by_text_or_shown(tmp_path: Path) -> None:
    pasted = legacy_marker("inbound-2")
    records = [
        row("USER_INPUT", content="<USER_REQUEST>hello</USER_REQUEST>"),
        row("USER_INPUT", content=f"<USER_REQUEST>\nhi\n{pasted}\n</USER_REQUEST>"),
    ]
    text = "".join(json.dumps(r) + "\n" for r in records)
    file_rows = [FileRow(0, 1, records[0]), FileRow(1, 2, records[1])]
    assert input_wire_ids(file_rows) == {}
    entries = EntryParser(UnreadRows()).entries(file_rows)
    wires = [e.wire_id for e in entries if isinstance(e, HumanInput)]
    assert wires == ["agy-unmarked:0", "agy-unmarked:1"]
    shown = window(write(tmp_path / "record.jsonl", text), harness="antigravity").timeline_items
    assert [item for item in shown if item.kind == "inbound_chat"] == []


def test_a_row_with_a_legacy_first_line_marker_is_named_in_the_same_namespace_as_a_bound_key() -> (
    None
):
    stamp = datetime.fromisoformat(STAMP.replace("Z", "+00:00"))
    legacy = row(
        "USER_INPUT", content=f"<USER_REQUEST>\n{legacy_marker('inbound-7')}\nold\n</USER_REQUEST>"
    )
    bound = row(
        "USER_INPUT", content="<USER_REQUEST>\nnew\n</USER_REQUEST>", meiva_input_key="inbound-8"
    )
    records = [FileRow(100, 200, legacy), FileRow(200, 300, bound)]
    assert input_wire_ids(records) == {
        wire_id("inbound-7"): InputRow(stamp),
        wire_id("inbound-8"): InputRow(stamp),
    }
    humans = [e for e in EntryParser(UnreadRows()).entries(records) if isinstance(e, HumanInput)]
    assert [(h.wire_id, h.text) for h in humans] == [
        (wire_id("inbound-7"), "old"),
        (wire_id("inbound-8"), "new"),
    ]


def test_antigravity_input_quote_resolves_the_first_matching_record_row(tmp_path: Path) -> None:
    content = "<USER_REQUEST>selected</USER_REQUEST>"
    line = json.dumps(row("USER_INPUT", content=content)) + "\n"
    path = tmp_path / "transcript_full.jsonl"
    _ = path.write_text(line + line)
    record_sent_input(
        inputs_path(path),
        key="inbound-1",
        min_offset=0,
        text_sha256=text_sha256("selected"),
        image_count=0,
    )
    file = AntigravityRecordFile(path)
    with file.quote_row(0, inbound=True) as found:
        assert found is not None and found.row.row["meiva_input_key"] == "inbound-1"
    with file.quote_row(len(line), inbound=True) as repeated:
        assert repeated is not None and "meiva_input_key" not in repeated.row.row


def test_antigravity_quote_reads_the_ledger_only_for_inputs(tmp_path: Path) -> None:
    message = json.dumps(row("PLANNER_RESPONSE", content="assistant")) + "\n"
    inbound = json.dumps(row("USER_INPUT", content="<USER_REQUEST>selected</USER_REQUEST>")) + "\n"
    path = tmp_path / "transcript_full.jsonl"
    _ = path.write_text(message + inbound)
    _ = inputs_path(path).write_bytes(b"x" * (BLOCK_BYTES + 1))
    file = AntigravityRecordFile(path)
    with file.quote_row(0, inbound=False) as assistant:
        assert assistant is not None and assistant.row.row["content"] == "assistant"
    with pytest.raises(RecordReadBudgetError), file.quote_row(len(message), inbound=True):
        pytest.fail("the quote reader must refuse an over-budget input ledger")
