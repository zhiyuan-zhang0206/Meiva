from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.lm.harness.antigravity.acp import prompt_blocks
from base.lm.harness.antigravity.transcript.file import AntigravityRecordFile
from base.lm.harness.antigravity.transcript.input_store import (
    ROW_KEY,
    Ledger,
    SentInput,
    bind_inputs,
    inputs_path,
    load_ledger,
    prompt_image_count,
    prompt_text_sha256,
    received_keys,
    record_bound_inputs,
    record_sent_input,
    resolve_inputs,
    row_input_key,
    text_sha256,
)
from base.lm.harness.delivery import ImageBlock, TextBlock


def request(text: str, footer: str = "") -> dict[str, object]:
    return {"type": "USER_INPUT", "content": f"<USER_REQUEST>\n{text}\n</USER_REQUEST>{footer}"}


def sent(key: str, text: str, floor: int = 0, images: int = 0) -> SentInput:
    return SentInput(key, floor, text_sha256(text), images)


def resolved(
    rows: list[dict[str, object]], *entries: SentInput, bound: dict[int, str] | None = None
):
    return resolve_inputs(enumerate(rows), Ledger(entries, bound or {}))


def test_the_prompt_carries_no_marker_and_hashes_to_what_the_cli_records() -> None:
    blocks = prompt_blocks([TextBlock("[t] a"), ImageBlock("image/png", "YQ=="), TextBlock("b\n")])
    assert blocks == [
        {"type": "text", "text": "[t] a"},
        {"type": "image", "mimeType": "image/png", "data": "YQ=="},
        {"type": "text", "text": "b\n"},
    ]
    assert "meiva-input" not in json.dumps(blocks)
    assert prompt_text_sha256(blocks) == text_sha256("[t] ab") and prompt_image_count(blocks) == 1
    assert prompt_blocks([ImageBlock("image/png", "YQ==")])[0]["type"] == "image"


def test_a_row_is_bound_by_the_digest_of_its_whole_text_never_by_a_part_of_it() -> None:
    rows = [request("[2026-11-26 Thu 15:06:52]\n\nB"), request("B")]
    assert resolved(rows, sent("one", "B")) == {1: "one"}
    assert resolved(rows, sent("part", "[2026-11-26 Thu 15:06:52]")) == {}


def test_equal_texts_sent_in_a_row_take_their_rows_one_to_one_in_file_order() -> None:
    rows = [request("again"), request("again"), request("again")]
    entries = (sent("a", "again"), sent("b", "again"), sent("c", "again"))
    assert resolved(rows, *entries) == {0: "a", 1: "b", 2: "c"}


def test_a_row_written_before_the_input_was_sent_is_not_its_row() -> None:
    rows = [request("B"), request("B")]
    assert resolved(rows, sent("k", "B", floor=1)) == {1: "k"}
    assert resolved(rows[:1], sent("k", "B", floor=1)) == {}


def test_an_earlier_binding_is_kept_and_its_sent_entry_is_not_consumed_again() -> None:
    rows = [request("again"), request("again")]
    entries = (sent("a", "again"), sent("b", "again"))
    assert resolved(rows, *entries, bound={0: "a"}) == {0: "a", 1: "b"}


def test_a_row_with_a_legacy_marker_is_never_taken_by_a_new_input() -> None:
    legacy = request(f"<!-- meiva-input:{'a' * 64} -->\nB")
    assert resolved([legacy], sent("k", "B")) == {}
    assert resolved([legacy], sent("k", f"<!-- meiva-input:{'a' * 64} -->\nB")) == {}


def test_an_image_only_input_is_bound_by_its_media_count() -> None:
    one: dict[str, object] = {
        **request("", "\n<ADDITIONAL_METADATA>uploaded</ADDITIONAL_METADATA>"),
        "media": [{}],
    }
    two: dict[str, object] = {**request(""), "media": [{}, {}]}
    assert resolved([one, two], sent("k", "", images=2)) == {1: "k"}


def test_text_with_images_and_a_footer_is_bound() -> None:
    row: dict[str, object] = {
        **request("look", "\n<ADDITIONAL_METADATA>time</ADDITIONAL_METADATA>"),
        "media": [{}],
    }
    assert resolved([row], sent("k", "look", images=1)) == {0: "k"}


def test_a_legacy_marker_names_a_key_only_among_the_keys_asked_about() -> None:
    legacy = request(f"<!-- meiva-input:{text_sha256('k1')} -->\nold")
    assert row_input_key(legacy, ["k1", "k2"]) == "k1"
    assert row_input_key(legacy, ["k2"]) is None
    assert received_keys([legacy, {"type": "USER_INPUT", ROW_KEY: "k2"}], ["k1", "k3"]) == {"k1"}
    assert row_input_key(request("plain"), ["k1"]) is None


def test_the_ledger_is_appended_before_a_crash_and_read_back(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "meiva-inputs.jsonl"
    record_sent_input(path, key="a", min_offset=0, text_sha256=text_sha256("x"), image_count=0)
    record_sent_input(path, key="b", min_offset=7, text_sha256=text_sha256("y"), image_count=1)
    record_bound_inputs(path, {3: "a"})
    record_bound_inputs(path, {3: "a", 9: "b"})
    ledger = load_ledger(path)
    assert [(s.key, s.min_offset, s.image_count) for s in ledger.sent] == [("a", 0, 0), ("b", 7, 1)]
    assert ledger.bound == {3: "a", 9: "b"}
    assert path.read_text().count('"kind":"bound"') == 2
    path.write_text(path.read_text() + '{"kind":"sent","ke')
    assert load_ledger(path) == ledger
    assert load_ledger(tmp_path / "absent.jsonl") == Ledger()


def test_a_ledger_line_of_an_unknown_shape_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "meiva-inputs.jsonl"
    path.write_text('{"kind":"other"}\n')
    with pytest.raises(ValueError, match="unexpected shape"):
        _ = load_ledger(path)


def test_rows_carry_their_key_and_the_record_file_resolves_a_sent_input_that_was_never_bound(
    tmp_path: Path,
) -> None:
    record = tmp_path / "transcript_full.jsonl"
    rows = [
        {"type": "PLANNER_RESPONSE", "status": "DONE", "step_index": 1, "created_at": "t"},
        {**request("1"), "status": "DONE", "step_index": 1, "created_at": "t"},
    ]
    record.write_text("".join(json.dumps(row) + "\n" for row in rows[:1]))
    size = record.stat().st_size
    record_sent_input(
        inputs_path(record), key="k", min_offset=size, text_sha256=text_sha256("1"), image_count=0
    )
    file = AntigravityRecordFile(record)
    assert ROW_KEY not in file.rows_after(0).rows[0].row
    record.write_text(record.read_text() + json.dumps(rows[1]) + "\n")
    after = file.rows_after(0).rows
    assert [row.row.get(ROW_KEY) for row in after] == [None, "k"]
    assert [row.get(ROW_KEY) for row in bind_inputs(record, after)[0]] == [None, "k"]
