from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

from base.agents.history.timeline_item import TimelineItem
from base.agents.history.transcript_timeline.items import format_tool_use
from base.agents.history.transcript_timeline.project import ProjectedItem, RecordProjector
from base.agents.history.transcript_timeline.record.harness import (
    entry_reader,
    reply_key,
    reply_key_prefix,
    session_file,
    unreadable_file,
    wire_id,
)
from base.agents.history.transcript_timeline.tests.support import POLICY, inbound, window
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.harness.session.transcript.record_file import BLOCK_BYTES, RecordFile
from base.log import logger

# meiva:note
# The timeline of a Codex thread read from its rollout: sanitized recordings of a real `codex app-server` 0.166.0
# (a plain turn with a command, a turn with an image, a message added to a running turn, a file change, an
# interrupt, a compaction and a resume; a web search and an image view; a spawned sub-agent; a fork), read through
# the same window and live paths the Claude tests use.
CODEX = "codex"
FIXTURES = Path(__file__).parents[4] / "lm/harness/codex/transcript/tests/fixtures/rollout"
INPUTS = {
    "cid-a1-1": 1,
    "cid-a1-2": 2,
    "cid-a1-3": 3,
    "cid-a1-steer": 4,
    "cid-a1-4": 5,
    "cid-a1-5": 6,
    "cid-a1-6": 7,
    "cid-a2-1": 8,
}
SESSION_KINDS = [
    "inbound_chat",
    "agent_chat",
    "agent_code",
    "code_output",
    "agent_chat",
    "inbound_chat",
    "agent_chat",
    "inbound_chat",
    "agent_chat",
    "inbound_chat",
    "agent_code",
    "code_output",
    "agent_chat",
    "inbound_chat",
    "agent_chat",
    "agent_code",
    "code_output",
    "agent_chat",
    "inbound_chat",
    "agent_chat",
    "system_marker",
    "inbound_chat",
    "agent_chat",
    "system_marker",
    "inbound_chat",
    "agent_chat",
]


def _text(name: str) -> str:
    return (FIXTURES / f"{name}.jsonl").read_text()


def _sent() -> dict[str, list[TimelineItem]]:
    return {key: [inbound(row, f"input {key}")] for key, row in INPUTS.items()}


def _read(tmp_path: Path, text: str, **kwargs: object) -> RecordFile:
    path = tmp_path / "rollout.jsonl"
    path.write_text(text)
    return RecordFile(path)


# meiva:note
# One more copy of a recording with every id renamed, so that copies stack into a longer thread without being taken
# for the same items.
def _generation(text: str, index: int) -> str:
    renamed = re.sub(r"\b(msg_|rs_|exec-|cid-)([\w-]+)", rf"\1g{index}x\2", text)
    return re.sub(
        r"\b([0-9a-f]{8})(-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
        rf"{index:08x}\2",
        renamed,
    )


def test_a_real_thread_reads_as_the_conversation_in_file_order(tmp_path: Path) -> None:
    text = _text("session")
    result = window(_read(tmp_path, text), harness=CODEX, sent=_sent())
    items = [projected.item for projected in result.items]
    assert [item.kind for item in items] == SESSION_KINDS
    assert [item.item_id for item in items if item.kind != "code_output"] == [
        f"h{projected.offset}.0"
        for projected in result.items
        if projected.item.kind != "code_output"
    ]
    starts, at = set[int](), 0
    for line in text.splitlines(keepends=True):
        starts.add(at)
        at += len(line.encode())
    assert {projected.offset for projected in result.items} <= starts
    assert [item.inbound_id for item in items if item.kind == "inbound_chat"] == [
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
    ]
    assert not result.has_more and result.size == len(text.encode())
    assert not any(item.activity_pending for item in items)


def test_a_message_added_to_a_running_turn_is_its_own_input_in_file_order(tmp_path: Path) -> None:
    items = window(_read(tmp_path, _text("session")), harness=CODEX, sent=_sent()).items
    texts = [(p.item.kind, p.item.payload) for p in items]
    steer = texts.index(("inbound_chat", "input cid-a1-steer"))
    assert texts[steer - 1][1] == "\u6211\u6b63\u5728\u8fd0\u884c\u6307\u5b9a\u547d\u4ee4\u3002"
    assert texts[steer + 1][0] == "agent_code", (
        "the command finished after the message was taken in"
    )
    assert len({p.turn for p in items[steer - 1 : steer + 4]}) == 1


def test_an_input_nobody_registered_is_not_shown(tmp_path: Path) -> None:
    items = window(_read(tmp_path, _text("session")), harness=CODEX).items
    assert "inbound_chat" not in [p.item.kind for p in items]


def test_a_file_change_a_web_search_an_image_view_and_a_sub_agent_are_calls_with_results(
    tmp_path: Path,
) -> None:
    session = window(_read(tmp_path, _text("session")), harness=CODEX, sent=_sent()).items
    patch = next(p for p in session if p.item.source == "patch")
    assert patch.item.payload == "# add: /work/note.txt\nhello"
    tools = window(
        _read(tmp_path, _text("tools")), harness=CODEX, sent={"cid-a3-1": [inbound(30)]}
    ).items
    assert [(p.item.kind, p.item.source) for p in tools if p.item.kind == "agent_code"] == [
        ("agent_code", "web_search"),
        ("agent_code", "view_image"),
    ]
    spawn = window(
        _read(tmp_path, _text("spawn")), harness=CODEX, sent={"cid-a2-3": [inbound(31)]}
    ).items
    call = next(p for p in spawn if p.item.source == "spawnAgent")
    result = spawn[spawn.index(call) + 1]
    assert result.item.kind == "code_output" and "01a1d14d-8c22" in result.item.payload
    report = spawn[-1].item
    assert (report.kind, report.source) == ("system_marker", "background_task")
    assert all(p.item.kind != "subagent_text" for p in spawn), (
        "the sub-agent's own record is not read"
    )


def test_a_fork_shows_only_its_own_rows(tmp_path: Path) -> None:
    items = window(
        _read(tmp_path, _text("fork")), harness=CODEX, sent={"cid-a2-2": [inbound(9)]}
    ).items
    assert [p.item.kind for p in items] == ["inbound_chat", "agent_chat"]


def test_paging_back_tiles_the_record(tmp_path: Path) -> None:
    file = _read(tmp_path, _text("session"))
    whole = [p.item.item_id for p in window(file, harness=CODEX, sent=_sent()).items]
    pages: list[list[str]] = []
    before: int | None = None
    while True:
        page = window(file, harness=CODEX, limit=5, before=before, sent=_sent())
        pages.append([p.item.item_id for p in page.items])
        if not page.has_more:
            break
        before = page.items[0].offset
    assert [i for page in reversed(pages) for i in page] == whole
    assert all(len(page) >= 5 for page in pages[:-1])


def test_a_window_reads_bytes_that_follow_its_size_not_the_files(tmp_path: Path) -> None:
    one = _text("session")
    reads: list[int] = []
    for copies in (10, 40):
        path = tmp_path / f"{copies}.jsonl"
        path.write_text("".join(_generation(one, i) for i in range(copies)))
        file = RecordFile(path)
        _ = window(file, harness=CODEX, limit=5)
        reads.append(file.bytes_read)
    assert reads[0] == reads[1], "a four times larger file reads the same bytes"
    assert reads[1] <= 2 * BLOCK_BYTES, "and no more than a block or two"


def _inflate(node: object, url: str) -> None:
    if is_object_dict(node):
        if node.get("type") in ("input_image", "image"):
            node["image_url"] = url
        for child in node.values():
            _inflate(child, url)
    elif is_object_list(node):
        for child in node:
            _inflate(child, url)


def test_a_picture_in_a_row_never_reaches_an_item(tmp_path: Path) -> None:
    rows = [json.loads(line) for line in _text("session").splitlines()]
    huge = "data:image/png;base64," + "A" * 4_000_000
    for row in rows:
        _inflate(row, huge)
    text = "".join(json.dumps(row) + "\n" for row in rows)
    assert len(text) > 8_000_000
    result = window(_read(tmp_path, text), harness=CODEX, sent=_sent())
    shown = json.dumps([p.item.model_dump() for p in result.items])
    assert "base64" not in shown and "AAAA" not in shown
    assert len(shown) < 20_000, "the items of a thread with a large picture are small"


def test_reading_the_file_as_it_grows_gives_the_items_a_cold_read_gives(tmp_path: Path) -> None:
    lines = [
        line
        for i in range(3)
        for line in (_generation(_text("session"), i) if i else _text("session")).splitlines(
            keepends=True
        )
    ]
    sent = {
        f"{key}" if i == 0 else key.replace("cid-", f"cid-g{i}x"): [inbound(row + 20 * i)]
        for i in range(3)
        for key, row in INPUTS.items()
    }
    path = tmp_path / "grow.jsonl"
    path.write_text("")
    file, at = RecordFile(path), 0
    shown: dict[str, ProjectedItem] = {}
    parser = entry_reader(CODEX)
    projector = RecordProjector(
        policy=POLICY, render_tool=format_tool_use, wire_id=lambda key: wire_id(CODEX, key)
    )
    for key, items in sent.items():
        projector.register(key, items)
    for start in range(0, len(lines), 7):
        with path.open("a") as handle:
            handle.write("".join(lines[start : start + 7]))
        block = file.rows_after(at)
        at = block.end
        for item in projector.feed(parser.entries(block.rows), tail=True, release_held=False).items:
            shown[item.item.item_id] = item
    cold = window(RecordFile(path), harness=CODEX, sent=sent).items
    assert [i.item.model_dump() for i in sorted(shown.values(), key=lambda i: i.offset)] == [
        i.item.model_dump() for i in cold
    ]


def _replies(items: Sequence[ProjectedItem]) -> list[str]:
    return [reply_key(CODEX, i.key) for i in items if i.item.kind == "agent_chat"]


def test_a_reply_has_one_publication_key_however_often_it_is_read(tmp_path: Path) -> None:
    file = _read(tmp_path, _text("session"))
    first = _replies(window(file, harness=CODEX, sent=_sent()).items)
    again = _replies(window(file, harness=CODEX, sent=_sent()).items)
    paged = _replies(window(file, harness=CODEX, limit=6, sent=_sent()).items)
    assert first == again and set(paged) <= set(first) and len(set(first)) == len(first)
    assert all(re.fullmatch(r"harness:codex:msg_\w+", key) for key in first)
    assert reply_key_prefix(CODEX) == "harness:codex:"
    assert not any(key.startswith(reply_key_prefix("claude_code")) for key in first)


def test_a_row_the_reader_does_not_know_is_skipped_and_reported_once(tmp_path: Path) -> None:
    unknown = '{"timestamp":"2026-11-12T19:45:25.361Z","ordinal":1,"type":"event_msg","payload":{"type":"mystery"}}\n'
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        text = unknown + _text("fork") + unknown + unknown
        items = window(_read(tmp_path, text), harness=CODEX, sent={"cid-a2-2": [inbound(9)]}).items
    finally:
        logger.remove(sink)
    assert [p.item.kind for p in items] == ["inbound_chat", "agent_chat"]
    assert len([m for m in messages if "event_msg/mystery" in m]) == 1


def test_a_file_that_is_there_in_another_form_is_told_from_one_that_is_not(
    tmp_path: Path,
) -> None:
    day = tmp_path / "sessions" / "2026" / "11" / "13"
    day.mkdir(parents=True)
    environ = {"CODEX_HOME": str(tmp_path)}
    thread = "01a1d14b-0f88-7ee3-8636-d3b8c1507a76"
    assert session_file(CODEX, environ, tmp_path, thread) is None
    assert unreadable_file(CODEX, environ, thread) is None
    (day / f"rollout-2026-11-13T03-45-19-{thread}.jsonl.zst").write_bytes(b"x")
    assert session_file(CODEX, environ, tmp_path, thread) is None
    assert (
        unreadable_file(CODEX, environ, thread) == f"rollout-2026-11-13T03-45-19-{thread}.jsonl.zst"
    )
    (day / f"rollout-2026-11-13T03-45-19-{thread}.jsonl").write_text("")
    found = session_file(CODEX, environ, tmp_path, thread)
    assert found is not None and found.name.endswith(f"{thread}.jsonl")
