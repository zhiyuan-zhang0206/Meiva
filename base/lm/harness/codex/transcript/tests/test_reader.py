from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

import pytest

from base.lm.harness.codex.transcript.history import PAGE_ITEMS
from base.lm.harness.codex.transcript.link import TranscriptLink
from base.lm.harness.codex.transcript.position import decode
from base.lm.harness.codex.transcript.reader import QUIET_RECHECK_SECONDS, CodexRecordReader
from base.lm.harness.codex.transcript.shapes import Obj
from base.lm.harness.codex.transcript.tests.history_fake import FakeHistory, write_lines
from base.lm.harness.codex.transcript.tests.support import (
    as_obj,
    load_json,
    load_lines,
    page_rows,
    project,
    row_item,
    sent,
    thread_turns,
    turn_items,
)
from base.lm.harness.session.transcript.entries import (
    MAIN_RECORD,
    AgentText,
    Anomaly,
    AnyEntry,
    HumanInput,
    SubagentLink,
    TurnEnd,
)
from base.lm.harness.session.transcript.ports import EntryBatch, PositionLostError, StoredRow

THREAD = "thr-main"
STAMP = "2026-11-12T08:57:13.042Z"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Rig:
    def __init__(self, tmp_path: Path, thread: str = THREAD) -> None:
        self.history = FakeHistory(thread)
        self.rollout = tmp_path / f"rollout-2026-11-12T10-00-00-{thread}.jsonl"
        self.rollout.touch()
        self.woken: list[str | None] = []
        self.link = TranscriptLink(self.woken.append)
        self.link.attach(self.history, rollout_path=str(self.rollout))
        self.clock = Clock()
        self.reader = CodexRecordReader(
            codex_home=tmp_path, thread_id=lambda: thread, link=self.link, now=self.clock
        )
        self.position: dict[str, str | None] = {}
        self.lines = 1000

    def feed(self, rows: Sequence[Obj], lines: Sequence[Obj] = ()) -> None:
        for row in rows:
            self.history.append(row)
        filler: Obj = {
            "type": "turn_context",
            "timestamp": STAMP,
            "ordinal": self.lines,
            "payload": {},
        }
        self.lines += 1
        write_lines(self.rollout, [*lines, filler])

    async def drain(self, record: str = MAIN_RECORD) -> list[EntryBatch]:
        batches: list[EntryBatch] = []
        while (
            batch := await self.reader.read_after(record, self.position.get(record))
        ) is not None:
            self.position[record] = batch.position
            batches.append(batch)
            assert len(batches) < 1000
        return batches


def entries_of(batches: list[EntryBatch]) -> list[AnyEntry]:
    return [entry for batch in batches for entry in batch.entries]


def basic() -> tuple[list[Obj], list[Obj]]:
    rows = [as_obj(row) for row in page_rows(copy.deepcopy(load_json("basic/items.json")))]
    return rows, load_lines("basic/rollout.jsonl")


async def test_a_thread_is_read_to_its_end_with_turn_ends_and_usage_after_the_items(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rows, lines = basic()
    rig.feed(rows, lines)
    batches = await rig.drain()
    entries = entries_of(batches)
    kinds = [type(entry).__name__ for entry in entries]
    assert kinds.count("TurnEnd") == 2 and kinds.count("TokenUsage") == 3
    last_item = max(i for i, k in enumerate(kinds) if k in ("AgentText", "ToolResult"))
    assert min(i for i, k in enumerate(kinds) if k in ("TurnEnd", "TokenUsage")) > last_item
    _, projection = project(entries, sent("cid-s1-a", "first"), sent("cid-s1-b", "second"))
    assert [closed.reason for closed in projection.turns_closed] == ["completed", "completed"]
    assert [delivery.input_key for delivery in projection.deliveries] == [
        "input-cid-s1-a",
        "input-cid-s1-b",
    ]
    assert not [entry for entry in entries if isinstance(entry, Anomaly)]
    stored = [row for batch in batches for row in batch.rows]
    assert [row.ordinal for row in stored] == sorted(row.ordinal for row in stored)
    assert all("item" in row.row or "payload" in row.row for row in stored)
    assert {r.record for r in stored} == {MAIN_RECORD}


async def test_an_idle_poll_asks_nothing_until_the_rollout_grows_or_a_recheck_is_due(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rows, lines = basic()
    rig.feed(rows, lines)
    _ = await rig.drain()
    asked = len(rig.history.calls)
    for _ in range(50):
        assert await rig.reader.read_after(MAIN_RECORD, rig.position[MAIN_RECORD]) is None
    assert len(rig.history.calls) == asked
    rig.clock.now += QUIET_RECHECK_SECONDS + 1
    assert await rig.reader.read_after(MAIN_RECORD, rig.position[MAIN_RECORD]) is None
    assert 0 < len(rig.history.calls) - asked <= 2


async def test_a_new_item_costs_one_small_request_and_a_steer_joins_the_turn_already_read(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rows, _ = basic()
    rig.feed(rows[:3], [])
    first = entries_of(await rig.drain())
    assert [type(e).__name__ for e in first] == ["HumanInput", "Reasoning", "AgentText"]
    before = len(rig.history.calls)
    steer = copy.deepcopy(rows[0])
    row_item(steer)["id"], row_item(steer)["clientId"] = "u-steer", "cid-steer"
    rig.feed([steer])
    (batch,) = await rig.drain()
    assert len(rig.history.calls) - before <= 2
    (human,) = batch.entries
    assert (
        isinstance(human, HumanInput) and human.joined_running_turn and human.wire_id == "cid-steer"
    )
    assert (
        decode(batch.position).history.inclusive
        and decode(batch.position).history.last == "u-steer"
    )


async def test_a_history_that_no_longer_holds_the_last_item_read_is_a_lost_position(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rows, lines = basic()
    rig.feed(rows, lines)
    _ = await rig.drain()
    last = decode(rig.position[MAIN_RECORD] or "").history.last
    assert last is not None
    rig.history.remove(last)
    rig.feed([copy.deepcopy(rows[1])], [])
    rig.clock.now += QUIET_RECHECK_SECONDS + 1
    with pytest.raises(PositionLostError):
        _ = await rig.reader.read_after(MAIN_RECORD, rig.position[MAIN_RECORD])


async def test_a_stored_position_that_is_not_ours_is_a_lost_position(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    for bad in ("not json", '{"v":2}', "[]"):
        with pytest.raises(PositionLostError):
            _ = await rig.reader.read_after(MAIN_RECORD, bad)


async def test_a_long_history_is_caught_up_in_pages_of_bounded_size(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rows, _ = basic()
    total = PAGE_ITEMS * 3 + 5
    items: list[Obj] = []
    for index in range(total):
        row = copy.deepcopy(rows[2])
        row_item(row)["id"] = f"msg-{index}"
        items.append(row)
    rig.feed(items)
    batches = await rig.drain()
    assert [len(batch.entries) for batch in batches] == [PAGE_ITEMS, PAGE_ITEMS, PAGE_ITEMS, 5, 0]
    texts = [e for e in entries_of(batches) if isinstance(e, AgentText)]
    assert [e.message_ref for e in texts] == [f"msg-{i}" for i in range(total)]
    assert decode(rig.position[MAIN_RECORD] or "").history.inclusive


async def test_a_child_thread_is_a_record_while_it_is_working_and_not_after(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    assert await rig.reader.records() == (MAIN_RECORD,)
    child = "thr-child"
    child_history = FakeHistory(child)
    child_rollout = tmp_path / f"rollout-2026-11-12T10-00-01-{child}.jsonl"
    child_rollout.touch()
    rows, _ = basic()
    child_history.append(rows[2])

    class Both:
        async def request(
            self, method: str, params: Mapping[str, object], *, timeout: float | None = None
        ) -> dict[str, object]:
            target = child_history if params["threadId"] == child else rig.history
            return await target.request(method, params, timeout=timeout)

    rig.link.attach(Both(), rollout_path=str(rig.rollout))
    rig.link.on_notification("item/completed", {"threadId": child, "turnId": "t"})
    assert rig.woken == [child]
    assert await rig.reader.records() == (MAIN_RECORD, child)
    batches = await rig.drain(child)
    assert [type(e).__name__ for e in entries_of(batches)] == ["AgentText"]
    assert {r.record for batch in batches for r in batch.rows} == {child}
    assert await rig.reader.records() == (MAIN_RECORD,)
    assert await rig.reader.records(exhaustive=True) == (MAIN_RECORD, child)
    rig.link.on_notification("item/outputDelta", {"threadId": child})
    assert await rig.reader.records() == (MAIN_RECORD, child)


async def test_a_spawn_call_makes_the_child_a_record_after_a_restart_rebuild(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    (turn,) = thread_turns(load_json("subagent/parent.json"))[:1]
    spawn = next(item for item in turn_items(turn) if item["type"] == "collabAgentToolCall")
    stored = StoredRow(
        MAIN_RECORD, 0, {"turnId": turn["id"], "item": spawn, "startedAtMs": 1, "completedAtMs": 2}
    )
    entries = rig.reader.parse_stored([stored])
    assert any(isinstance(e, SubagentLink) for e in entries)
    assert len(await rig.reader.records()) == 2


async def test_a_record_is_seeded_at_the_first_input_this_process_sent(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rows, lines = basic()
    rig.feed(rows, lines)
    position = await rig.reader.position_before(MAIN_RECORD, ("cid-s1-b",))
    assert position is not None
    entries = entries_of([batch async for batch in _from(rig, MAIN_RECORD, position)])
    humans = [e for e in entries if isinstance(e, HumanInput)]
    assert [h.wire_id for h in humans] == ["cid-s1-b"]
    assert not [e for e in entries if isinstance(e, TurnEnd) and e.turn != row_turn(rows, -1)]
    end = await rig.reader.position_before(MAIN_RECORD, ())
    assert end is not None
    assert entries_of([batch async for batch in _from(rig, MAIN_RECORD, end)]) == []


async def _from(rig: Rig, record: str, position: str) -> AsyncIterator[EntryBatch]:
    rig.position[record] = position
    for batch in await rig.drain(record):
        yield batch


def row_turn(rows: list[Obj], index: int) -> str:
    return str(rows[index]["turnId"])


async def test_the_reader_asks_only_for_item_pages_never_for_a_whole_history(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rows, lines = basic()
    rig.feed(rows, lines)
    _ = await rig.drain()
    _ = await rig.reader.position_before(MAIN_RECORD, ("cid-s1-b",))
    assert set(rig.history.methods) == {"thread/items/list"}
    assert all("limit" in params for _, params in rig.history.calls)
    assert max(int(str(params["limit"])) for _, params in rig.history.calls) <= PAGE_ITEMS * 2
