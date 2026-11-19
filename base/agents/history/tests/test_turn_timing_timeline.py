from __future__ import annotations

from datetime import UTC, datetime, timedelta

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from base.agents.history.timeline import build_timeline_items
from base.agents.history.timeline_item import (
    TurnTimingContext,
    close_turn_timing_update,
    open_turn_timing_update,
)
from base.agents.messages.timestamp_policy import TimestampPolicy


def test_open_and_close_turn_timing_helpers() -> None:
    t0 = datetime(2026, 11, 19, 7, 0, 0, tzinfo=UTC)
    assert (
        close_turn_timing_update(
            active_turn_id=None,
            active_turn_started_at=None,
            active_turn_start_idx=None,
            turn_timings=[],
            end_idx=3,
            ended_at=t0,
        )
        == {}
    )

    opened, tid, started_iso = open_turn_timing_update(start_idx=1, started_at=t0)
    assert opened["active_turn_id"] == tid
    assert opened["active_turn_started_at"] == started_iso
    assert opened["active_turn_start_idx"] == 1

    t1 = t0 + timedelta(seconds=12, milliseconds=345)
    closed = close_turn_timing_update(
        active_turn_id=tid,
        active_turn_started_at=started_iso,
        active_turn_start_idx=1,
        turn_timings=[],
        end_idx=4,
        ended_at=t1,
    )
    assert closed["active_turn_id"] is None
    assert closed["active_turn_started_at"] is None
    assert closed["active_turn_start_idx"] is None
    assert closed["turn_timings"] == [
        {
            "turn_id": tid,
            "start_idx": 1,
            "end_idx": 4,
            "started_at": started_iso,
            "ended_at": t1.isoformat(),
            "duration_ms": 12345,
        }
    ]


def test_build_timeline_items_attributes_closed_and_active_turns_without_idle_gaps() -> None:
    policy = TimestampPolicy.off()
    messages = [
        SystemMessage(content="sys"),
        HumanMessage(
            content="hi",
            additional_kwargs={
                "meiva_msg_type": "inbound",
                "meiva_created_at": "2026-11-19T07:00:00Z",
            },
        ),
        AIMessage(
            content="",
            tool_calls=[{"id": "c1", "name": "bash", "args": {"code": "ls"}}],
            additional_kwargs={"meiva_created_at": "2026-11-19T07:00:02Z"},
        ),
        AIMessage(
            content="",
            tool_calls=[{"id": "c2", "name": "bash", "args": {"code": "pwd"}}],
            additional_kwargs={"meiva_created_at": "2026-11-19T07:30:05Z"},
        ),
        AIMessage(
            content="still thinking",
            additional_kwargs={"meiva_created_at": "2026-11-19T07:35:00Z"},
        ),
    ]
    timings = [
        {"turn_id": "t1", "start_idx": 1, "end_idx": 3, "duration_ms": 4200},
        {"turn_id": "t2", "start_idx": 3, "end_idx": 4, "duration_ms": 8100},
    ]

    items, msg_count = build_timeline_items(
        messages,
        inputs=policy,
        timing=TurnTimingContext(
            timings=timings,
            active_turn_id="t3",
            active_turn_start_idx=4,
        ),
    )
    assert msg_count == 5
    by_id = {item.item_id: item for item in items}

    assert by_id["0.0"].turn_id is None
    assert by_id["1.0"].turn_id is None
    assert by_id["2.0"].turn_id == "t1"
    assert by_id["2.0"].turn_duration_ms == 4200
    assert by_id["3.0"].turn_id == "t2"
    assert by_id["3.0"].turn_duration_ms == 8100
    assert by_id["4.0"].turn_id == "t3"
    assert by_id["4.0"].turn_duration_ms is None


def test_build_timeline_items_shifts_turn_timings_for_historical_segments() -> None:
    policy = TimestampPolicy.off()
    stripped_messages = [
        AIMessage(content="done", additional_kwargs={"meiva_created_at": "2026-11-19T07:00:05Z"}),
    ]
    timings = [
        {"turn_id": "t-hist", "start_idx": 1, "end_idx": 2, "duration_ms": 5600},
    ]
    items, _ = build_timeline_items(
        stripped_messages,
        inputs=policy,
        segment_prefix="s1.ckpt1",
        timing=TurnTimingContext(timings=timings, index_shift=-1),
    )
    assert len(items) == 1
    assert items[0].item_id == "s1.ckpt1.0.0"
    assert items[0].turn_id == "t-hist"
    assert items[0].turn_duration_ms == 5600
