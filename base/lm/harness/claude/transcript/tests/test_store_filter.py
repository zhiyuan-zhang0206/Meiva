from __future__ import annotations

import hashlib
import json

import pytest

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.harness.claude.transcript.parse import ParseState, parse_row
from base.lm.harness.claude.transcript.scrub import scrub
from base.lm.harness.claude.transcript.store_filter import (
    INPUT_SIZE_KEY,
    MAIN_TEXT_CAP_BYTES,
    SUBAGENT_TEXT_CAP_BYTES,
    stored_row,
)
from base.lm.harness.claude.transcript.tests.support import rows
from base.lm.harness.session.transcript.entries import AnyEntry

SCENARIOS = (
    "basic",
    "multiblock",
    "steer",
    "interrupt",
    "kill",
    "resume",
    "denied",
    "subagents",
    "compact",
    "fork",
    "own_turn",
    "api_error",
)


def _content(kept: dict[str, object]) -> list[dict[str, object]]:
    message = kept["message"]
    assert is_object_dict(message)
    blocks = message["content"]
    assert is_object_list(blocks)
    result = [block for block in blocks if is_object_dict(block)]
    assert len(result) == len(blocks)
    return result


def _entries(numbered: list[tuple[int, dict[str, object]]]) -> list[AnyEntry]:
    state = ParseState()
    found: list[AnyEntry] = []
    for line, raw in numbered:
        state, parsed = parse_row(state, raw, line=line)
        found.extend(parsed)
    return found


@pytest.mark.parametrize("scenario", SCENARIOS)
# meiva:note
# Whole entries (text, tool output, status, duration, order, turn), not only (type, key).
def test_the_parser_reads_the_same_entries_from_the_stored_rows_as_from_the_raw_rows(
    scenario: str,
) -> None:
    raw_rows = list(enumerate(rows(scenario)))
    kept = [
        (line, filtered)
        for line, raw in raw_rows
        if (filtered := stored_row(raw, subagent=False)) is not None
    ]
    assert _entries(kept) == _entries(raw_rows)
    assert len(json.dumps([row for _, row in kept])) <= len(json.dumps(rows(scenario)))


def test_bookkeeping_context_and_unknown_rows_are_not_stored() -> None:
    for raw in (
        {"type": "api-request", "uuid": "a", "blob": "x" * 100},
        {"type": "queue-operation", "operation": "enqueue"},
        {"type": "session-owner", "accountUuid": "acct"},
        {"type": "attachment", "attachment": {"type": "skill_listing", "content": "x"}},
        {"type": "system", "subtype": "something_else", "uuid": "s"},
        {"type": "a-type-from-the-future", "uuid": "f"},
    ):
        assert stored_row(raw, subagent=False) is None


def test_fields_the_projection_does_not_read_are_dropped_and_the_rest_kept_as_written() -> None:
    raw = {
        "type": "assistant",
        "uuid": "u1",
        "timestamp": "2026-11-12T09:00:00.000Z",
        "cwd": "/somewhere",
        "sessionId": "s",
        "parentUuid": "p",
        "requestId": "req",
        "message": {
            "id": "m1",
            "model": "m",
            "stop_reason": "end_turn",
            "role": "assistant",
            "usage": {"input_tokens": 1, "output_tokens": 2, "iterations": [{"big": "x"}]},
            "content": [{"type": "thinking", "thinking": "t", "signature": "sig" * 100}],
        },
    }
    kept = stored_row(raw, subagent=False)
    assert kept == {
        "type": "assistant",
        "uuid": "u1",
        "timestamp": "2026-11-12T09:00:00.000Z",
        "message": {
            "id": "m1",
            "model": "m",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 2},
            "content": [{"type": "thinking", "thinking": "t"}],
        },
    }


def test_image_bytes_are_replaced_by_a_reference_and_big_text_is_cut_with_its_length() -> None:
    big = "x" * (MAIN_TEXT_CAP_BYTES + 10)
    raw = {
        "type": "user",
        "uuid": "u",
        "timestamp": "2026-11-12T09:00:00.000Z",
        "message": {
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
                },
                {"type": "tool_result", "tool_use_id": "t", "content": big},
                {"type": "text", "text": big},
            ]
        },
    }
    kept = stored_row(raw, subagent=False)
    assert kept is not None
    content = _content(kept)
    assert content[0] == {
        "type": "image",
        "sha256": hashlib.sha256(b"QUJD").hexdigest(),
        "bytes": 4,
        "media_type": "image/png",
    }
    cut = content[1]["content"]
    assert isinstance(cut, str) and len(cut) == MAIN_TEXT_CAP_BYTES
    assert content[1]["truncated_from_bytes"] == MAIN_TEXT_CAP_BYTES + 10
    assert content[2]["truncated_from_bytes"] == MAIN_TEXT_CAP_BYTES + 10
    assert "QUJD" not in json.dumps(kept)


def _big_input(count: int, size: int) -> dict[str, object]:
    return {"edits": [{"old_string": "a" * size, "new_string": "b" * size} for _ in range(count)]}


def test_a_sub_agent_field_is_cut_at_the_smaller_cap_and_a_main_field_at_the_larger() -> None:
    text = "x" * (SUBAGENT_TEXT_CAP_BYTES * 3)
    raw = {
        "type": "user",
        "uuid": "u",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": text}]},
    }
    for subagent, cap in ((True, SUBAGENT_TEXT_CAP_BYTES), (False, MAIN_TEXT_CAP_BYTES)):
        kept = stored_row(raw, subagent=subagent)
        assert kept is not None
        block = _content(kept)[0]
        body = block["content"]
        assert isinstance(body, str) and len(body) == min(cap, len(text))
        assert (block.get("truncated_from_bytes") == len(text)) == (cap < len(text))
    assert SUBAGENT_TEXT_CAP_BYTES == 16_384 and MAIN_TEXT_CAP_BYTES == 262_144


def test_a_tool_input_is_bounded_as_a_whole_including_nested_structures() -> None:
    raw = {
        "type": "assistant",
        "uuid": "a",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "c1",
                    "name": "MultiEdit",
                    "input": _big_input(40, 5000),
                },
                {
                    "type": "tool_use",
                    "id": "c2",
                    "name": "Edit",
                    "input": {"file_path": "/a/b.py", "old_string": "o" * 100, "n": 3},
                },
                {
                    "type": "tool_use",
                    "id": "c3",
                    "name": "Bash",
                    "input": {"command": "c" * (SUBAGENT_TEXT_CAP_BYTES * 2)},
                },
            ]
        },
    }
    kept = stored_row(raw, subagent=True)
    assert kept is not None
    multi, small, bash = (block["input"] for block in _content(kept))
    assert is_object_dict(multi)
    size = multi[INPUT_SIZE_KEY]
    assert isinstance(size, int) and size > 400_000
    edits = multi["edits"]
    assert is_object_dict(edits) and edits["type"] == "list" and edits["bytes"] == size - 11
    assert small == {"file_path": "/a/b.py", "old_string": "o" * 100, "n": 3}
    assert isinstance(bash, dict) and INPUT_SIZE_KEY in bash
    assert len(json.dumps(bash)) <= SUBAGENT_TEXT_CAP_BYTES + 200
    for block in _content(kept):
        assert len(json.dumps(block["input"])) <= SUBAGENT_TEXT_CAP_BYTES + 200


def test_error_and_origin_are_kept_only_in_the_form_the_parser_reads() -> None:
    raw = {
        "type": "user",
        "uuid": "u",
        "origin": {"kind": "task-notification", "accountUuid": "acct", "extra": {"a": 1}},
        "error": {"message": "x" * 5000, "accountUuid": "acct"},
        "message": {"content": "hi"},
    }
    kept = stored_row(raw, subagent=False)
    assert kept is not None
    assert kept["origin"] == {"kind": "task-notification"} and "error" not in kept
    assert "acct" not in json.dumps(kept)
    plain = stored_row({**raw, "error": "overloaded_error"}, subagent=False)
    assert plain is not None and plain["error"] == "overloaded_error"


def test_nul_and_lone_surrogates_are_removed_everywhere_so_the_row_is_storable() -> None:
    raw = {
        "type": "user",
        "uuid": "u\x00",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "t", "content": "a\x00b\ud83dc"},
                {"type": "tool_use", "id": "c", "name": "n", "input": {"k\x00": ["v\x00"]}},
            ]
        },
    }
    kept = stored_row(raw, subagent=False)
    assert kept is not None
    text = json.dumps(kept)
    assert "\\u0000" not in text and "\\ud83d" not in text
    assert kept["uuid"] == "u"
    assert _content(kept)[0]["content"] == "ab\ufffdc"
    assert _content(kept)[1]["input"] == {"k": ["v"]}
    assert scrub("a\x00b") == "ab"


def test_scrub_survives_nesting_deeper_than_the_stack() -> None:
    deep: object = "leaf\x00"
    for _ in range(100_000):
        deep = [deep]
    holder = {"v": deep}
    _ = scrub(holder)
    node: object = holder["v"]
    while isinstance(node, list):
        node = node[0]
    assert node == "leaf"


def test_a_queued_notification_keeps_the_kind_of_its_origin_and_nothing_else_of_it() -> None:
    raw = {
        "type": "attachment",
        "uuid": "q1",
        "timestamp": "2026-11-12T09:00:00Z",
        "attachment": {
            "type": "queued_command",
            "prompt": "p",
            "source_uuid": "s",
            "origin": {"kind": "task-notification", "accountUuid": "acct"},
            "runId": "r",
        },
    }
    kept = stored_row(raw, subagent=False)
    assert kept is not None
    assert kept["attachment"] == {
        "type": "queued_command",
        "source_uuid": "s",
        "origin": {"kind": "task-notification"},
        "prompt": "p",
    }
