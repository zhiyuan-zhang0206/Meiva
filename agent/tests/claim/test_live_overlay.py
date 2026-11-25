from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import psycopg

from agent.graph.claim.harness_read_sink import ReadTimelineSink
from agent.ownership.tests.ownership_support import agent_row
from base.agents.context import MeivaContext
from base.api_contracts.agent_contract import Harness
from base.clock.tests.fakes import system_clock
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.tests.live_capture import LiveEvents
from base.events.live.tests.live_capture import live_events as live_events
from base.host.boundary.fields import str_field
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from base.host.env.agent_slices import AgentSlices
from base.lm.harness import BridgeEvent, ReasoningDelta, TextDelta

FIXTURES = Path(__file__).parents[3] / "base/lm/harness/claude/tests/fixtures/streaming"
SESSION = "streaming-session"


@dataclass(frozen=True)
class _Snapshot:
    committed: list[tuple[str, str]]
    retires: list[str]


@dataclass(frozen=True)
class _Seen:
    started: list[tuple[str, str]]
    text: dict[str, str]
    snapshots: list[_Snapshot]
    last_delta: int
    snapshot_at: list[int]

    @property
    def retired(self) -> list[str]:
        return [id_ for snapshot in self.snapshots for id_ in snapshot.retires]

    def committed(self, kind: str) -> list[str]:
        return [
            payload
            for snapshot in self.snapshots
            for item_kind, payload in snapshot.committed
            if item_kind == kind
        ]


def _seen(received: list[dict[str, object]]) -> _Seen:
    started: list[tuple[str, str]] = []
    text: dict[str, str] = {}
    snapshots: list[_Snapshot] = []
    snapshot_at: list[int] = []
    last_delta = -1
    for index, event in enumerate(received):
        role = str_field(event, "role")
        if role in ("chat_start", "reasoning_start"):
            started.append((role, str_field(event, "item_id")))
        elif role in ("chat_delta", "reasoning_delta"):
            item_id = str_field(event, "item_id")
            text[item_id] = text.get(item_id, "") + str_field(event, "content")
            last_delta = index
        elif role == "timeline_snapshot":
            snapshot_at.append(index)
            snapshots.append(_snapshot(event))
    return _Seen(started, text, snapshots, last_delta, snapshot_at)


def _snapshot(event: dict[str, object]) -> _Snapshot:
    items, retires = event["items"], event.get("retires", [])
    assert is_object_list(items) and is_object_list(retires)
    committed: list[tuple[str, str]] = []
    for item in items:
        assert is_object_dict(item)
        kind = str_field(item, "kind")
        if kind in ("agent_chat", "agent_reasoning"):
            committed.append((kind, str_field(item, "payload")))
    return _Snapshot(committed, [str(id_) for id_ in retires])


def _rows(scenario: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in (FIXTURES / scenario / "record.jsonl").read_text().splitlines():
        wrapper = object_of(json.loads, line)
        assert is_object_dict(wrapper)
        decoded = wrapper["row"]
        assert is_object_dict(decoded)
        message = decoded.get("message")
        if (
            decoded["type"] == "user"
            and is_object_dict(message)
            and isinstance(message["content"], str)
        ):
            continue
        rows.append(decoded)
    return rows


def _stream_events(scenario: str, turn_id: str = "t1") -> list[BridgeEvent]:
    message_id, events = "", list[BridgeEvent]()
    for line in (FIXTURES / scenario / "stream.jsonl").read_text().splitlines():
        entry = object_of(json.loads, line)
        assert is_object_dict(entry)
        event = entry.get("event")
        if not is_object_dict(event):
            continue
        if event["type"] == "message_start":
            message = event["message"]
            assert is_object_dict(message)
            message_id = str_field(message, "id")
        elif event["type"] == "content_block_delta":
            delta, block = event["delta"], f"{message_id}:{event['index']}"
            assert is_object_dict(delta)
            if delta["type"] == "text_delta":
                events.append(TextDelta(turn_id, str_field(delta, "text"), block))
            elif delta["type"] == "thinking_delta":
                events.append(ReasoningDelta(turn_id, str_field(delta, "thinking"), block))
    return events


def _sink(
    tmp_path: Path, live_events: LiveEvents, gate: ProcessDbGate, bus: EventBus, agent_id: int
) -> tuple[ReadTimelineSink, Path]:
    ctx = MeivaContext(
        agent=AgentSlices.resolve({"user_delivery_mode": "naive"}, default_reader=defaults()),
        harness=Harness.CLAUDE_CODE,
        meiva_home=tmp_path,
        db=Database.from_settings(gate=gate),
        bus=bus,
        clock_factory=system_clock,
        environ={"HOME": str(tmp_path)},
    )
    record = tmp_path / ".claude/projects/project" / f"{SESSION}.jsonl"
    record.parent.mkdir(parents=True)
    record.touch()
    sink = ReadTimelineSink(
        ctx, agent_id, live_events.publisher, non_human_wake=False, session_id=lambda: SESSION
    )
    return sink, record


def _append(record: Path, rows: list[dict[str, object]]) -> None:
    with record.open("a") as handle:
        for row in rows:
            _ = handle.write(json.dumps(row) + "\n")


async def _replay(
    sink: ReadTimelineSink, events: list[BridgeEvent], *, drop: int | None = None
) -> None:
    for index, event in enumerate(events):
        if index != drop:
            await sink.on_event(event)


async def test_the_streamed_text_is_the_committed_text_and_commit_retires_it(
    tmp_path: Path,
    live_events: LiveEvents,
    database_gate: ProcessDbGate,
    db_conn: psycopg.Connection,
    event_bus: EventBus,
) -> None:
    sink, record = _sink(tmp_path, live_events, database_gate, event_bus, agent_row(db_conn))
    await _replay(sink, _stream_events("text_only"))
    _append(record, _rows("text_only"))
    await sink.finish()
    seen = _seen(await live_events.object_events())
    [(role, live_id)] = seen.started
    assert role == "chat_start" and live_id.startswith("live.t1.msg_") and live_id.endswith(":0")
    assert seen.committed("agent_chat") == [seen.text[live_id]] == ["4 is the answer to 2+2."]
    [snapshot] = seen.snapshots
    assert snapshot.retires == [live_id]
    assert seen.snapshot_at[0] > seen.last_delta


async def test_a_turn_streams_each_block_under_its_own_id_and_retires_them_with_their_rows(
    tmp_path: Path,
    live_events: LiveEvents,
    database_gate: ProcessDbGate,
    db_conn: psycopg.Connection,
    event_bus: EventBus,
) -> None:
    sink, record = _sink(tmp_path, live_events, database_gate, event_bus, agent_row(db_conn))
    await _replay(sink, _stream_events("thinking_text_tool"))
    _append(record, _rows("thinking_text_tool"))
    await sink.finish()
    seen = _seen(await live_events.object_events())
    assert [role for role, _ in seen.started] == ["reasoning_start", "chat_start"] * 2
    assert seen.retired == [item_id for _, item_id in seen.started]
    for role, item_id in seen.started:
        kind = "agent_chat" if role == "chat_start" else "agent_reasoning"
        assert seen.text[item_id] in seen.committed(kind)


async def test_a_dropped_delta_does_not_change_the_committed_item(
    tmp_path: Path,
    live_events: LiveEvents,
    database_gate: ProcessDbGate,
    db_conn: psycopg.Connection,
    event_bus: EventBus,
) -> None:
    sink, record = _sink(tmp_path, live_events, database_gate, event_bus, agent_row(db_conn))
    await _replay(sink, _stream_events("text_only"), drop=2)
    _append(record, _rows("text_only"))
    await sink.finish()
    seen = _seen(await live_events.object_events())
    [(_, live_id)] = seen.started
    assert seen.committed("agent_chat") == ["4 is the answer to 2+2."]
    assert seen.text[live_id] != "4 is the answer to 2+2."
    assert seen.retired == [live_id]


async def test_an_interrupted_text_is_committed_by_its_partial_row_and_retired_with_it(
    tmp_path: Path,
    live_events: LiveEvents,
    database_gate: ProcessDbGate,
    db_conn: psycopg.Connection,
    event_bus: EventBus,
) -> None:
    sink, record = _sink(tmp_path, live_events, database_gate, event_bus, agent_row(db_conn))
    await _replay(sink, _stream_events("interrupt_text"))
    _append(record, _rows("interrupt_text"))
    await sink.finish(interrupted=True)
    seen = _seen(await live_events.object_events())
    [(_, live_id)] = seen.started
    assert seen.retired == [live_id]
    assert seen.committed("agent_chat")
    [committed] = seen.committed("agent_chat")
    assert seen.text[live_id].startswith(committed.removesuffix("[truncated]").rstrip())


async def test_an_interrupted_thinking_leaves_no_row_and_the_turn_end_retires_it(
    tmp_path: Path,
    live_events: LiveEvents,
    database_gate: ProcessDbGate,
    db_conn: psycopg.Connection,
    event_bus: EventBus,
) -> None:
    sink, record = _sink(tmp_path, live_events, database_gate, event_bus, agent_row(db_conn))
    await _replay(sink, _stream_events("interrupt_thinking"))
    _append(record, _rows("interrupt_thinking"))
    await sink.finish(interrupted=True)
    seen = _seen(await live_events.object_events())
    [(_, live_id)] = seen.started
    assert seen.retired == [live_id]
    assert seen.committed("agent_reasoning") == []


async def test_a_delta_that_arrives_after_its_block_was_retired_shows_nothing(
    tmp_path: Path,
    live_events: LiveEvents,
    database_gate: ProcessDbGate,
    db_conn: psycopg.Connection,
    event_bus: EventBus,
) -> None:
    sink, record = _sink(tmp_path, live_events, database_gate, event_bus, agent_row(db_conn))
    deltas = _stream_events("text_only")
    await _replay(sink, deltas[:2])
    _append(record, _rows("text_only"))
    await sink.finish()
    _ = await live_events.object_events()
    await _replay(sink, deltas[2:])
    await sink.finish()
    after = _seen(await live_events.object_events())
    assert after.started == [] and after.text == {}
