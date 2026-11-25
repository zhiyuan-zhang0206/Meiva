from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.harness.codex.frames import as_object
from base.lm.harness.codex.tests.support import Harness, start
from base.lm.harness.delivery.tests.sent import send
from base.lm.harness.events import (
    AssistantText,
    ReasoningDelta,
    ReasoningText,
    TextDelta,
    TurnCompleted,
)

STREAMING = Path(__file__).parent / "fixtures" / "streaming"
_SKIPPED = {"turn/started", "thread/status/changed"}


def _lines(scenario: str, name: str) -> list[dict[str, object]]:
    path = STREAMING / scenario / name
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _record_replies(scenario: str) -> dict[str, str]:
    replies: dict[str, str] = {}
    for line in _lines(scenario, "record.jsonl"):
        payload = as_object(as_object(line["row"]).get("payload"))
        item = as_object(payload.get("item"))
        if payload.get("type") == "item_completed" and item.get("type") == "AgentMessage":
            parts = item.get("content")
            assert is_object_list(parts)
            replies[str(item["id"])] = "".join(str(as_object(part)["text"]) for part in parts)
    return replies


def _replay_stream(harness: Harness, scenario: str) -> None:
    server = harness.server
    for line in _lines(scenario, "stream.jsonl"):
        method = line.get("method")
        if not isinstance(method, str) or method in _SKIPPED:
            continue
        text = json.dumps(line["params"])
        text = text.replace("$thread", server.thread_id).replace("$turn", server.current_turn)
        params = json.loads(text)
        assert is_object_dict(params)
        server.emit(method, params)


async def _replayed(tmp_path: Path, scenario: str) -> Harness:
    harness = await start(tmp_path)
    ticket = await send(harness, "replay")
    await harness.turn_started()
    _replay_stream(harness, scenario)
    await asyncio.wait_for(ticket.outcome(), 2)
    return harness


def _streamed(harness: Harness) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for delta in harness.of(TextDelta):
        assert delta.block is not None
        blocks[delta.block] = blocks.get(delta.block, "") + delta.text
    return blocks


@pytest.mark.parametrize("scenario", ["text_only", "commentary_command_final"])
async def test_streamed_text_equals_the_record_of_the_same_item(
    tmp_path: Path, scenario: str
) -> None:
    harness = await _replayed(tmp_path, scenario)
    try:
        streamed, recorded = _streamed(harness), _record_replies(scenario)
        assert streamed
        assert streamed == recorded
        finished = [event.text for event in harness.of(AssistantText)]
        assert finished == list(recorded.values())
    finally:
        await harness.stop()


async def test_a_reasoning_summary_is_not_streamed_and_is_still_reported_whole(
    tmp_path: Path,
) -> None:
    harness = await _replayed(tmp_path, "reasoning_summary")
    try:
        assert not harness.of(ReasoningDelta)
        assert [event.text for event in harness.of(ReasoningText)]
        assert _streamed(harness) == _record_replies("reasoning_summary")
    finally:
        await harness.stop()


async def test_an_interrupted_reply_streams_but_never_reaches_the_record(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        ticket = await send(harness, "replay")
        await harness.turn_started()
        _replay_stream(harness, "interrupt_text")
        await asyncio.wait_for(ticket.outcome(), 2)
        completed = await harness.wait_for(lambda event: isinstance(event, TurnCompleted))
        assert completed.interrupted
        assert _streamed(harness)
        assert not harness.of(AssistantText)
        assert not _record_replies("interrupt_text")
    finally:
        await harness.stop()
