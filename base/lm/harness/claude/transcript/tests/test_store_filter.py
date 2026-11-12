from __future__ import annotations

import hashlib
import json

import pytest

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.harness.claude.transcript.parse import ParseState, parse_row
from base.lm.harness.claude.transcript.store_filter import ROW_TEXT_CAP_BYTES, stored_row
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


def _entries(raw_rows: list[dict[str, object]]) -> list[tuple[str, str]]:
    state = ParseState()
    found: list[AnyEntry] = []
    for line, raw in enumerate(raw_rows):
        state, parsed = parse_row(state, raw, line=line)
        found.extend(parsed)
    return [(type(entry).__name__, entry.key) for entry in found]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_parser_reads_the_same_entries_from_the_stored_rows_as_from_the_raw_rows(
    scenario: str,
) -> None:
    raw_rows = rows(scenario)
    kept = [filtered for raw in raw_rows if (filtered := stored_row(raw)) is not None]
    assert _entries(kept) == _entries(raw_rows)
    assert len(json.dumps(kept)) <= len(json.dumps(raw_rows))


def test_bookkeeping_context_and_unknown_rows_are_not_stored() -> None:
    for raw in (
        {"type": "api-request", "uuid": "a", "blob": "x" * 100},
        {"type": "queue-operation", "operation": "enqueue"},
        {"type": "session-owner", "accountUuid": "acct"},
        {"type": "attachment", "attachment": {"type": "skill_listing", "content": "x"}},
        {"type": "system", "subtype": "something_else", "uuid": "s"},
        {"type": "a-type-from-the-future", "uuid": "f"},
    ):
        assert stored_row(raw) is None


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
    kept = stored_row(raw)
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
    big = "x" * (ROW_TEXT_CAP_BYTES + 10)
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
    kept = stored_row(raw)
    assert kept is not None
    content = _content(kept)
    assert content[0] == {
        "type": "image",
        "sha256": hashlib.sha256(b"QUJD").hexdigest(),
        "bytes": 4,
        "media_type": "image/png",
    }
    cut = content[1]["content"]
    assert isinstance(cut, str) and len(cut) == ROW_TEXT_CAP_BYTES
    assert content[1]["truncated_from_bytes"] == ROW_TEXT_CAP_BYTES + 10
    assert content[2]["truncated_from_bytes"] == ROW_TEXT_CAP_BYTES + 10
    assert "QUJD" not in json.dumps(kept)
