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
from base.lm.harness import BridgeEvent, TextDelta

FIXTURES = Path(__file__).parents[3] / "base/lm/harness/codex/tests/fixtures/streaming"
THREAD = "thread-1"


def _lines(scenario: str, name: str) -> list[dict[str, object]]:
    lines: list[dict[str, object]] = []
    for line in (FIXTURES / scenario / name).read_text().splitlines():
        decoded = object_of(json.loads, line.replace("$thread", THREAD).replace("$turn", "turn-1"))
        assert is_object_dict(decoded)
        lines.append(decoded)
    return lines


def _rows(scenario: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in _lines(scenario, "record.jsonl"):
        row = line["row"]
        assert is_object_dict(row)
        rows.append(row)
    return rows


def _stream_events(scenario: str) -> list[BridgeEvent]:
    events: list[BridgeEvent] = []
    for line in _lines(scenario, "stream.jsonl"):
        params = line.get("params")
        if line.get("method") == "item/agentMessage/delta" and is_object_dict(params):
            events.append(TextDelta("t1", str_field(params, "delta"), str_field(params, "itemId")))
    return events


@dataclass(frozen=True)
class _Seen:
    started: list[str]
    text: dict[str, str]
    committed: list[str]
    retired: list[str]
    last_delta: int
    snapshot_at: int


def _seen(received: list[dict[str, object]]) -> _Seen:
    started: list[str] = []
    text: dict[str, str] = {}
    committed: list[str] = []
    retired: list[str] = []
    last_delta, snapshot_at = -1, len(received)
    for index, event in enumerate(received):
        role = str_field(event, "role")
        if role == "chat_start":
            started.append(str_field(event, "item_id"))
        elif role == "chat_delta":
            item_id = str_field(event, "item_id")
            text[item_id] = text.get(item_id, "") + str_field(event, "content")
            last_delta = index
        elif role == "timeline_snapshot":
            snapshot_at = min(snapshot_at, index)
            items, retires = event["items"], event.get("retires", [])
            assert is_object_list(items) and is_object_list(retires)
            retired.extend(str(id_) for id_ in retires)
            committed.extend(
                str_field(item, "payload")
                for item in items
                if is_object_dict(item) and str_field(item, "kind") == "agent_chat"
            )
    return _Seen(started, text, committed, retired, last_delta, snapshot_at)


def _append(record: Path, rows: list[dict[str, object]]) -> None:
    with record.open("a") as handle:
        for row in rows:
            _ = handle.write(json.dumps(row) + "\n")


async def _replay(sink: ReadTimelineSink, events: list[BridgeEvent]) -> None:
    for event in events:
        await sink.on_event(event)


def _sink(
    tmp_path: Path, live_events: LiveEvents, gate: ProcessDbGate, bus: EventBus, agent_id: int
) -> tuple[ReadTimelineSink, Path]:
    ctx = MeivaContext(
        agent=AgentSlices.resolve({"user_delivery_mode": "naive"}, default_reader=defaults()),
        harness=Harness.CODEX,
        meiva_home=tmp_path,
        db=Database.from_settings(gate=gate),
        bus=bus,
        clock_factory=system_clock,
        environ={"HOME": str(tmp_path)},
    )
    record = tmp_path / ".codex/sessions/2026/11/25" / f"rollout-2026-11-25T03-26-23-{THREAD}.jsonl"
    record.parent.mkdir(parents=True)
    record.touch()
    sink = ReadTimelineSink(
        ctx, agent_id, live_events.publisher, non_human_wake=False, session_id=lambda: THREAD
    )
    return sink, record


async def test_each_codex_message_streams_under_its_item_id_and_is_retired_by_its_row(
    tmp_path: Path,
    live_events: LiveEvents,
    database_gate: ProcessDbGate,
    db_conn: psycopg.Connection,
    event_bus: EventBus,
) -> None:
    sink, record = _sink(tmp_path, live_events, database_gate, event_bus, agent_row(db_conn))
    await _replay(sink, _stream_events("commentary_command_final"))
    _append(record, _rows("commentary_command_final"))
    await sink.finish()
    seen = _seen(await live_events.object_events())
    assert len(seen.started) == 2
    assert seen.retired == seen.started
    assert [seen.text[item_id] for item_id in seen.started] == seen.committed
    assert seen.snapshot_at > seen.last_delta


async def test_an_interrupted_codex_reply_is_retired_by_the_turn_end_and_never_committed(
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
    [live_id] = seen.started
    assert seen.text[live_id]
    assert seen.retired == [live_id]
    assert seen.committed == []
