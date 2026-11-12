from __future__ import annotations

import json
from pathlib import Path

from base.agents.history.timeline_item import TimelineItem
from base.agents.history.transcript_timeline.items import format_tool_use
from base.agents.history.transcript_timeline.read import MAX_REQUEST_BYTES, read_window
from base.agents.history.transcript_timeline.tests.support import (
    POLICY,
    fixture_text,
    generation,
    inbound,
    window,
    with_wire_ids,
    write,
)

BASIC_KINDS = [
    "inbound_chat",
    "agent_reasoning",
    "agent_code",
    "code_output",
    "agent_reasoning",
    "agent_chat",
]


def _offsets(text: str) -> set[int]:
    offsets, at = set[int](), 0
    for line in text.splitlines(keepends=True):
        offsets.add(at)
        at += len(line.encode())
    return offsets


def test_a_real_turn_is_named_by_the_offsets_of_its_rows(tmp_path: Path) -> None:
    text = fixture_text("basic")
    file = write(tmp_path / "basic.jsonl", text)
    result = window(file, sent={"k1": [inbound(5)]})
    items = [projected.item for projected in result.items]
    assert [item.kind for item in items] == BASIC_KINDS
    assert all(projected.offset in _offsets(text) for projected in result.items)
    assert [item.item_id for item in items] == [
        f"h{projected.offset}.0" for projected in result.items
    ]
    assert items[0].inbound_id == 5
    assert items[0].source == "user"
    assert not result.has_more
    assert result.size == len(text.encode())


def test_an_input_nobody_registered_is_not_shown(tmp_path: Path) -> None:
    file = write(tmp_path / "basic.jsonl", fixture_text("basic"))
    items = window(file).items
    assert [projected.item.kind for projected in items] == BASIC_KINDS[1:]


def test_one_input_of_several_messages_shows_one_item_per_message(tmp_path: Path) -> None:
    text = with_wire_ids(fixture_text("basic"), {"3c3d2de5-c479-52b7-91a6-fce4e4d26db9": "group"})
    file = write(tmp_path / "group.jsonl", text)
    items = window(file, sent={"group": [inbound(7, "first"), inbound(8, "second")]}).items
    shown = [projected.item for projected in items if projected.item.kind == "inbound_chat"]
    assert [(item.inbound_id, item.payload) for item in shown] == [(7, "first"), (8, "second")]
    assert shown[0].item_id.split(".")[0] == shown[1].item_id.split(".")[0]
    assert [item.item_id.split(".")[1] for item in shown] == ["0", "1"]


def test_pages_before_a_cursor_tile_the_record_without_gaps_or_repeats(tmp_path: Path) -> None:
    text = fixture_text("long_session")
    file = write(tmp_path / "long.jsonl", text)
    whole = [p.item.item_id for p in window(file, limit=1000).items]
    pages: list[str] = []
    before: int | None = None
    while True:
        page = window(file, limit=4, before=before)
        ids = [p.item.item_id for p in page.items]
        pages = [*ids, *pages]
        if not page.has_more:
            break
        before = page.items[0].offset
    assert pages == whole
    assert len(whole) > 8


def test_a_call_without_a_result_is_running_until_its_result_is_in_the_record(
    tmp_path: Path,
) -> None:
    lines = fixture_text("basic").splitlines(keepends=True)
    call_at = next(i for i, line in enumerate(lines) if '"type":"tool_use"' in line)
    partial = write(tmp_path / "partial.jsonl", "".join(lines[: call_at + 1]))
    call = next(p.item for p in window(partial).items if p.item.kind == "agent_code")
    assert (call.activity_pending, call.activity_revision) == (True, 1)
    done = write(tmp_path / "done.jsonl", "".join(lines))
    settled = next(p.item for p in window(done).items if p.item.kind == "agent_code")
    assert (settled.activity_pending, settled.activity_revision) == (None, None)
    assert settled.item_id == call.item_id, "the same item, updated in place"


def test_the_window_of_an_old_page_shows_no_call_as_running(tmp_path: Path) -> None:
    text = fixture_text("basic")
    file = write(tmp_path / "basic.jsonl", text)
    last = window(file).items[-1]
    older = window(file, before=last.offset).items
    assert all(p.item.activity_pending is None for p in older)


def test_an_interrupted_turn_shows_the_interrupt_the_record_wrote(tmp_path: Path) -> None:
    file = write(tmp_path / "interrupt.jsonl", fixture_text("interrupt"))
    payloads = [p.item.payload for p in window(file).items if p.item.kind == "system_marker"]
    assert "status:Turn interrupted" in payloads


def test_a_request_reads_about_the_window_and_not_the_file(tmp_path: Path) -> None:
    turn = fixture_text("basic")
    small = write(tmp_path / "small.jsonl", "".join(generation(turn, i) for i in range(1, 40)))
    large = write(tmp_path / "large.jsonl", "".join(generation(turn, i) for i in range(1, 1600)))
    assert large.size() > 40 * small.size()
    _ = window(small, limit=20)
    _ = window(large, limit=20)
    assert large.bytes_read <= 2 * small.bytes_read
    assert large.bytes_read < large.size() / 40


def test_a_result_is_read_together_with_its_call(tmp_path: Path) -> None:
    turn = fixture_text("basic")
    file = write(tmp_path / "pairs.jsonl", "".join(generation(turn, i) for i in range(1, 30)))
    page = window(file, limit=3)
    kinds = [p.item.kind for p in page.items]
    assert kinds[0] != "code_output", "a window never starts with the result of a call it lacks"


def _filler(megabytes: float) -> str:
    row = json.dumps({"type": "api-request-blob", "blob": "x" * 100_000}) + "\n"
    return row * int(megabytes * 1024 * 1024 / len(row))


def test_the_inbox_is_asked_only_about_a_stretch_with_a_persons_input(tmp_path: Path) -> None:
    lines = fixture_text("basic").splitlines(keepends=True)
    asked: list[int] = []

    def ask(_since: object, _until: object) -> dict[str, list[TimelineItem]]:
        asked.append(1)
        return {}

    def read(text: str) -> None:
        _ = read_window(
            write(tmp_path / "f.jsonl", text),
            limit=50,
            before=None,
            policy=POLICY,
            render_tool=format_tool_use,
            load_sent=ask,
        )

    read("".join(line for line in lines if '"role":"user"' not in line or "tool_result" in line))
    assert asked == [], "no person's input in the rows, no query"
    read("".join(lines))
    assert asked != []


def test_a_sparse_record_costs_work_in_proportion_to_the_range_it_has_to_cover(
    tmp_path: Path,
) -> None:
    turn = fixture_text("basic")
    file = write(tmp_path / "sparse.jsonl", generation(turn, 1) + _filler(3) + generation(turn, 2))
    found = window(file, limit=50)
    assert [p.item.kind for p in found.items].count("agent_chat") == 2, "both turns are reached"
    assert file.bytes_read <= 3 * file.size(), (
        "the look-back doubles: a small multiple of the range"
    )


def test_a_request_never_reads_past_its_hard_cap_and_says_there_is_more(tmp_path: Path) -> None:
    file = write(tmp_path / "huge.jsonl", generation(fixture_text("basic"), 1) + _filler(40))
    found = window(file, limit=50)
    assert found.items == () and found.has_more
    assert file.bytes_read <= MAX_REQUEST_BYTES + 1024 * 1024
