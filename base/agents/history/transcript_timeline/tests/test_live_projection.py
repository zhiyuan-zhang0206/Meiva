from __future__ import annotations

from pathlib import Path

from base.agents.history.transcript_timeline.items import format_tool_use
from base.agents.history.transcript_timeline.project import ProjectedItem, RecordProjector
from base.agents.history.transcript_timeline.tests.support import (
    POLICY,
    fixture_text,
    generation,
    inbound,
    window,
)
from base.lm.harness.claude.transcript.window import EntryParser, RecordFile, wire_id


# meiva:note
# What a live reader keeps: the file's offset, the parser and the projector, nothing else.
class Follower:
    def __init__(self, file: RecordFile) -> None:
        self.file = file
        self.at = 0
        self.parser = EntryParser()
        self.projector = RecordProjector(
            policy=POLICY, render_tool=format_tool_use, wire_id=wire_id
        )
        self.shown: dict[str, ProjectedItem] = {}
        self.reads: list[int] = []

    def read(self) -> None:
        before = self.file.bytes_read
        block = self.file.rows_after(self.at)
        self.at = block.end
        projected = self.projector.feed(
            self.parser.entries(block.rows), tail=True, release_held=False
        )
        for item in projected.items:
            self.shown[item.item.item_id] = item
        self.reads.append(self.file.bytes_read - before)


def _grow(path: Path, lines: list[str]) -> None:
    with path.open("a") as handle:
        handle.write("".join(lines))


def _dump(items: list[ProjectedItem]) -> list[dict[str, object]]:
    return [item.item.model_dump() for item in sorted(items, key=lambda it: it.offset)]


def test_reading_the_file_as_it_grows_gives_the_items_a_cold_read_gives(tmp_path: Path) -> None:
    lines = [
        *fixture_text("basic").splitlines(keepends=True),
        *(
            line
            for i in range(1, 6)
            for line in generation(fixture_text("basic"), i).splitlines(keepends=True)
        ),
    ]
    path = tmp_path / "grow.jsonl"
    path.write_text("")
    follower = Follower(RecordFile(path))
    follower.projector.register("k1", [inbound(5)])
    for start in range(0, len(lines), 4):
        _grow(path, lines[start : start + 4])
        follower.read()
    cold = window(RecordFile(path), sent={"k1": [inbound(5)]}).items
    assert _dump(list(follower.shown.values())) == _dump(list(cold))


def test_a_running_call_is_settled_by_the_read_that_finds_its_result(tmp_path: Path) -> None:
    lines = fixture_text("basic").splitlines(keepends=True)
    call_at = next(i for i, line in enumerate(lines) if '"type":"tool_use"' in line)
    path = tmp_path / "call.jsonl"
    path.write_text("")
    follower = Follower(RecordFile(path))
    _grow(path, lines[: call_at + 1])
    follower.read()
    running = next(i for i in follower.shown.values() if i.is_call)
    assert running.item.activity_pending is True
    _grow(path, lines[call_at + 1 :])
    follower.read()
    settled = follower.shown[running.item.item_id]
    assert settled.item.activity_pending is None
    assert settled.item.item_id == running.item.item_id


def test_a_read_in_a_long_turn_costs_the_new_bytes_not_the_turn(tmp_path: Path) -> None:
    lines = fixture_text("basic").splitlines(keepends=True)
    call_at = next(i for i, line in enumerate(lines) if '"type":"tool_use"' in line)
    pair = lines[call_at : call_at + 2]
    path = tmp_path / "long.jsonl"
    path.write_text("")
    follower = Follower(RecordFile(path))
    _grow(path, lines[:call_at])
    follower.read()
    for step in range(60):
        _grow(
            path,
            [
                line
                for i in range(10)
                for line in generation("".join(pair), step * 10 + i + 1).splitlines(keepends=True)
            ],
        )
        follower.read()
    assert follower.file.size() > 2 * 1024 * 1024 // 4
    steady = follower.reads[5:]
    assert max(steady) <= 1.5 * min(steady), "the bytes of a read do not grow with the turn"
    assert max(steady) < follower.file.size() / 4


def test_a_reply_has_one_publication_identity_however_it_is_read(tmp_path: Path) -> None:
    path = tmp_path / "reply.jsonl"
    path.write_text(fixture_text("basic"))
    cold = window(RecordFile(path)).items
    follower = Follower(RecordFile(path))
    follower.read()
    again = RecordProjector(policy=POLICY, render_tool=format_tool_use, wire_id=wire_id)
    entries = EntryParser().entries(RecordFile(path).rows_after(0).rows)
    replay = again.feed(entries, tail=True, release_held=False).items

    def keys(items: list[ProjectedItem] | tuple[ProjectedItem, ...]) -> list[str]:
        return [i.key for i in items if i.item.kind == "agent_chat"]

    assert keys(cold) == keys(list(follower.shown.values())) == keys(replay)
    assert len(keys(cold)) == 1 and keys(cold)[0].startswith("claude:")
