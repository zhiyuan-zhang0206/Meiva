from __future__ import annotations

import base64
import hashlib
import json

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from base.lm.harness.codex.transcript.parse import parse_item_row, parse_stored_row
from base.lm.harness.codex.transcript.store_filter import stored_item_row, stored_rollout_line
from base.lm.harness.codex.transcript.tests.support import (
    as_obj,
    load_json,
    load_lines,
    page_rows,
    thread_turns,
    turn_items,
)
from base.lm.harness.session.jsonl.store_shapes import INPUT_SIZE_KEY
from base.lm.harness.session.jsonl.store_text import (
    MAIN_TEXT_CAP_BYTES,
    SUBAGENT_TEXT_CAP_BYTES,
    scrub,
)
from base.lm.harness.session.transcript.entries import MAIN_RECORD

# meiva:note
# What of Codex's record is kept, and that what is kept is enough: every recorded history row and rollout line is
# filtered, re-read with the stored-row parser and must give the entries the unfiltered row gave (so a restart or
# a corrected parser reproduces the timeline from the database alone); the sizes and the shapes that must never be
# stored (image bytes, text over the cap, a tool's whole arguments) are held to their caps.


def _row(item: dict[str, object]) -> dict[str, object]:
    return {
        "turnId": "t1",
        "item": {"id": "i1", "startedAtMs": 1, "completedAtMs": 2, **item},
        "startedAtMs": 1,
        "completedAtMs": 2,
    }


def _stored(item: dict[str, object], *, sub: bool = False) -> dict[str, object]:
    kept = stored_item_row(_row(item), subagent=sub)
    assert kept is not None
    stored = kept["item"]
    assert is_object_dict(stored)
    return stored


def test_every_recorded_row_is_stored_in_a_form_that_reads_back_to_the_same_entries() -> None:
    rows = [
        as_obj(row)
        for name in ("basic", "late_command")
        for row in page_rows(load_json(f"{name}/items.json"))
    ]
    parent = load_json("subagent/parent.json")
    rows += [
        {
            "turnId": turn["id"],
            "item": item,
            "startedAtMs": item["startedAtMs"],
            "completedAtMs": item.get("completedAtMs"),
        }
        for turn in thread_turns(parent)
        for item in turn_items(turn)
    ]
    assert len(rows) > 15
    for ordinal, row in enumerate(rows):
        direct = parse_item_row(
            as_obj(object_of(json.loads, json.dumps(row))), ordinal, MAIN_RECORD, set()
        )
        kept = stored_item_row(row, subagent=False)
        assert kept is not None, row["item"]
        again = parse_stored_row(kept, ordinal, MAIN_RECORD, set())
        assert [(type(e), e.key) for e in again] == [(type(e), e.key) for e in direct]


def test_every_recorded_rollout_line_the_reader_asks_for_reads_back_to_the_same_entries() -> None:
    kept = 0
    for name in ("basic", "steer", "subagent", "late_command"):
        for line in load_lines(f"{name}/rollout.jsonl"):
            stored = stored_rollout_line(line)
            if stored is None:
                continue
            kept += 1
            direct = parse_stored_row(
                as_obj(object_of(json.loads, json.dumps(line))), 0, MAIN_RECORD, set()
            )
            again = parse_stored_row(stored, 0, MAIN_RECORD, set())
            assert [(type(e), e.key) for e in again] == [(type(e), e.key) for e in direct]
            assert "encrypted_content" not in json.dumps(stored)
    assert kept > 8


def test_the_lines_the_history_already_has_or_nobody_reads_are_not_stored() -> None:
    lines = load_lines("subagent/rollout.jsonl")
    stored_types = {
        (line["type"], as_obj(line["payload"]).get("type"))
        for line in lines
        if stored_rollout_line(line) is not None
    }
    assert stored_types <= {
        ("event_msg", "task_complete"),
        ("event_msg", "turn_aborted"),
        ("token_usage_record", None),
        ("response_item", "message"),
    }
    assert all(
        stored_rollout_line(line) is None
        for line in lines
        if line["type"] in ("world_state", "turn_context", "compacted", "session_meta")
    )


def test_an_inline_image_is_stored_as_its_hash_and_size_never_its_bytes() -> None:
    pixels = base64.b64encode(b"\x89PNG" + bytes(2000)).decode()
    url = f"data:image/png;base64,{pixels}"
    content = [{"type": "text", "text": "look"}, {"type": "image", "url": url, "detail": None}]
    stored = _stored({"type": "userMessage", "clientId": "cid", "content": content})
    parts = stored["content"]
    assert is_object_list(parts) and len(parts) == 2
    text_part, image = parts[0], parts[1]
    assert text_part == {"type": "text", "text": "look"}
    assert image == {
        "type": "image",
        "sha256": hashlib.sha256(url.encode()).hexdigest(),
        "bytes": len(url),
        "media_type": "image/png",
    }
    assert pixels not in json.dumps(stored)
    link = _stored(
        {"type": "userMessage", "content": [{"type": "image", "url": "https://x/y.png"}]}
    )
    assert link["content"] == [{"type": "image", "url": "https://x/y.png"}]


def test_text_is_cut_at_the_cap_of_its_record_with_its_original_length() -> None:
    big = "é" * (MAIN_TEXT_CAP_BYTES)
    main = _stored({"type": "agentMessage", "text": big, "phase": "final_answer"})
    assert len(str(main["text"]).encode()) <= MAIN_TEXT_CAP_BYTES
    assert main["truncated_from_bytes"] == len(big.encode())
    sub = _stored({"type": "agentMessage", "text": big}, sub=True)
    assert len(str(sub["text"]).encode()) <= SUBAGENT_TEXT_CAP_BYTES
    assert sub["truncated_from_bytes"] == len(big.encode())
    command = _stored(
        {
            "type": "commandExecution",
            "command": "ls",
            "status": "completed",
            "aggregatedOutput": "o" * 40_000,
        },
        sub=True,
    )
    assert len(str(command["aggregatedOutput"])) == SUBAGENT_TEXT_CAP_BYTES
    short = _stored({"type": "agentMessage", "text": "fits"})
    assert "truncated_from_bytes" not in short


def test_the_arguments_of_a_tool_are_bounded_as_a_whole() -> None:
    arguments = {"query": "q", "blob": "b" * 500_000, "nested": {"k": ["v"] * 5_000}}
    stored = _stored(
        {
            "type": "mcpToolCall",
            "server": "s",
            "tool": "t",
            "status": "completed",
            "arguments": arguments,
        }
    )
    kept = stored["arguments"]
    assert is_object_dict(kept)
    size = kept[INPUT_SIZE_KEY]
    assert isinstance(size, int) and size > 500_000
    assert len(json.dumps(kept)) <= MAIN_TEXT_CAP_BYTES
    assert kept["query"] == "q"


def test_a_row_with_nul_or_a_lone_surrogate_is_made_storable_before_it_is_kept() -> None:
    row = _row({"type": "agentMessage", "text": "a\x00b\ud800c"})
    _ = scrub(row)
    kept = stored_item_row(row, subagent=False)
    assert kept is not None
    assert as_obj(kept["item"])["text"] == "ab�c"
    _ = json.dumps(kept).encode("utf-8")


def test_a_type_the_parser_does_not_turn_into_entries_is_not_stored() -> None:
    for kind in ("hookPrompt", "functionCallOutput", "subAgentActivity", "holographicLedger"):
        assert stored_item_row(_row({"type": kind, "output": "secret"}), subagent=False) is None
    assert stored_item_row({"turnId": "t", "item": "not an object"}, subagent=False) is None


def test_a_failed_turn_keeps_its_error_and_a_usage_line_only_its_counts() -> None:
    complete = {
        "type": "event_msg",
        "timestamp": "t",
        "ordinal": 3,
        "payload": {
            "type": "task_complete",
            "turn_id": "t1",
            "last_agent_message": "private",
            "error": {"message": "nope", "codex_error_info": "x", "additionalDetails": "private"},
        },
    }
    kept = stored_rollout_line(complete)
    assert kept is not None
    assert as_obj(kept["payload"])["error"] == {"message": "nope", "codex_error_info": "x"}
    assert "private" not in json.dumps(kept)
    usage = {
        "type": "token_usage_record",
        "timestamp": "t",
        "ordinal": 4,
        "payload": {
            "turn_id": "t1",
            "response_id": "r",
            "usage": {"input_tokens": 5, "output_tokens": 1, "model_prompt": "private"},
            "turn_token_usage": {"input_tokens": 99},
        },
    }
    kept = stored_rollout_line(usage)
    assert kept is not None
    assert as_obj(kept["payload"]) == {
        "turn_id": "t1",
        "response_id": "r",
        "usage": {"input_tokens": 5, "output_tokens": 1},
    }
