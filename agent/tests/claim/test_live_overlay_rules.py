from __future__ import annotations

import json

from agent.graph.llm_stream.harness_overlay import LiveOverlay
from base.agents.history.timeline_item import TimelineItem
from base.agents.history.transcript_timeline.project import ProjectedItem
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.lm.harness import AssistantText, ReasoningDelta, TextDelta, ToolUse


def _committed(kind: str, text: str, *, block: str | None = None) -> ProjectedItem:
    return ProjectedItem(
        key="k",
        offset=0,
        item=TimelineItem(
            item_id="h0.0", kind="agent_chat" if kind == "chat" else "agent_reasoning", payload=text
        ),
        turn=None,
        call_id=None,
        is_call=False,
        block=block,
        text=text,
    )


def _overlay() -> tuple[LiveOverlay, list[dict[str, object]]]:
    sent: list[dict[str, object]] = []

    def collect(payload: str) -> None:
        decoded = object_of(json.loads, payload)
        assert is_object_dict(decoded)
        sent.append(decoded)

    overlay = LiveOverlay(
        collect,
        7,
        non_human_wake=lambda: False,
        explicit_visibility=True,
        delivery_mode=None,
    )
    return overlay, sent


async def test_a_stream_without_block_ids_is_one_block_until_its_text_is_final() -> None:
    overlay, sent = _overlay()
    for text in ("\nHello", " wor", "ld"):
        overlay.on_event(TextDelta(turn_id="t1", text=text))
    overlay.on_event(AssistantText(turn_id="t1", text="Hello world"))
    overlay.on_event(TextDelta(turn_id="t1", text="Second"))
    first, second = overlay.live_ids
    assert first == "live.t1.n0" and second == "live.t1.n1"
    assert overlay.superseded([_committed("chat", "Hello world")]) == [first]
    assert [e["role"] for e in sent if e["role"] == "chat_start"] == ["chat_start", "chat_start"]
    assert all(e["explicit_visibility"] is True for e in sent if e["role"] == "chat_start")


async def test_a_tool_call_ends_the_open_block_of_a_stream_without_block_ids() -> None:
    overlay, _ = _overlay()
    overlay.on_event(TextDelta(turn_id="t1", text="Looking"))
    overlay.on_event(ToolUse(turn_id="t1", tool_use_id="c1", name="Read", tool_input={}))
    overlay.on_event(TextDelta(turn_id="t1", text="Found it"))
    assert overlay.live_ids == ["live.t1.n0", "live.t1.n1"]


async def test_a_committed_text_that_does_not_extend_the_streamed_one_retires_nothing() -> None:
    overlay, _ = _overlay()
    overlay.on_event(TextDelta(turn_id="t1", text="Hello"))
    assert overlay.superseded([_committed("chat", "Goodbye")]) == []
    assert overlay.superseded([_committed("reasoning", "Hello")]) == []


async def test_blocks_with_different_ids_never_match_by_text() -> None:
    overlay, _ = _overlay()
    overlay.on_event(TextDelta(turn_id="t1", text="Hello", block="m1:0"))
    assert overlay.superseded([_committed("chat", "Hello", block="m2:0")]) == []
    assert overlay.superseded([_committed("chat", "Hello", block="m1:0")]) == ["live.t1.m1:0"]


async def test_a_block_the_record_committed_first_is_not_shown_when_its_deltas_arrive_late() -> (
    None
):
    overlay, sent = _overlay()
    assert overlay.superseded([_committed("chat", "Hello", block="m1:0")]) == []
    overlay.on_event(TextDelta(turn_id="t1", text="Hello", block="m1:0"))
    assert sent == [] and overlay.live_ids == []


async def test_a_block_does_not_come_back_after_it_was_retired_or_its_turn_ended() -> None:
    overlay, sent = _overlay()
    overlay.on_event(TextDelta(turn_id="t1", text="a", block="m1:0"))
    assert overlay.superseded([_committed("chat", "a", block="m1:0")]) == ["live.t1.m1:0"]
    overlay.on_event(TextDelta(turn_id="t1", text="b", block="m1:0"))
    overlay.on_event(TextDelta(turn_id="t1", text="c", block="m1:1"))
    assert overlay.retire_all() == ["live.t1.m1:1"]
    overlay.on_event(TextDelta(turn_id="t1", text="d", block="m1:2"))
    assert overlay.live_ids == []
    assert [e["role"] for e in sent].count("chat_start") == 2


async def test_pending_deltas_are_sent_before_the_retirement_is_returned() -> None:
    overlay, sent = _overlay()
    overlay.on_event(TextDelta(turn_id="t1", text="tail", block="m1:0"))
    assert [e["role"] for e in sent] == ["chat_start"]
    _ = overlay.retire_all()
    assert [e["role"] for e in sent] == ["chat_start", "chat_delta"]


async def test_a_committed_block_retires_the_unmatched_earlier_blocks_of_its_message() -> None:
    overlay, _ = _overlay()
    overlay.on_event(ReasoningDelta(turn_id="t1", text="unreadable", block="m1:0"))
    overlay.on_event(TextDelta(turn_id="t1", text="Hello", block="m1:1"))
    overlay.on_event(TextDelta(turn_id="t1", text="Later", block="m1:2"))
    overlay.on_event(ReasoningDelta(turn_id="t1", text="Other", block="m2:0"))
    assert overlay.superseded([_committed("chat", "Hello", block="m1:1")]) == [
        "live.t1.m1:0",
        "live.t1.m1:1",
    ]
    assert overlay.live_ids == ["live.t1.m1:2", "live.t1.m2:0"]


async def test_a_late_delta_of_a_block_retired_as_unmatched_shows_nothing() -> None:
    overlay, sent = _overlay()
    overlay.on_event(ReasoningDelta(turn_id="t1", text="unreadable", block="m1:0"))
    assert overlay.superseded([_committed("chat", "Hello", block="m1:1")]) == ["live.t1.m1:0"]
    sent.clear()
    overlay.on_event(ReasoningDelta(turn_id="t1", text="more", block="m1:0"))
    assert sent == [] and overlay.live_ids == []
