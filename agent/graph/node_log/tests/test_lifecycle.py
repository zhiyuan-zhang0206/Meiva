import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from agent.graph import node_log
from agent.graph.node_log import StallConfig, node_lifecycle
from base.agents.messages.kwargs import MeivaMsgType
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.agents.observation.turn_progress import TurnProgress
from base.clock import Clock
from base.events.live.tests.live_capture import LiveEvents
from base.log.tests.log_records import LogRecords

_TIMELINE_INPUTS = TimestampPolicy(clock=Clock.from_settings(), timestamps_enabled=False)
_TIMELINE_LIMIT = 100


def _enter(
    live: LiveEvents,
    messages: list[BaseMessage],
    *,
    node: str = "llm",
    full_window: bool = False,
    timeline_inputs: TimestampPolicy = _TIMELINE_INPUTS,
    timeline_limit: int = _TIMELINE_LIMIT,
):
    return node_lifecycle(
        node,
        messages=messages,
        event_publisher=live.publisher,
        agent_id=1,
        turn_progress=TurnProgress(),
        stall=StallConfig(0.0),
        timeline_inputs=timeline_inputs,
        timeline_limit=timeline_limit,
        full_window=full_window,
    )


def _filler(count: int) -> list[BaseMessage]:
    return [SystemMessage(content="x")] * count


def _history(*texts: str) -> list[BaseMessage]:
    return [SystemMessage(content="prompt"), *(AIMessage(content=text) for text in texts)]


def _records(records: LogRecords, event: str) -> list[dict[str, Any]]:
    return [r for r in records if r["extra"].get("event") == event]


def _snapshots(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event["role"] == "timeline_snapshot"]


def _item_ids(snapshot: dict[str, Any]) -> list[str]:
    return [item["item_id"] for item in snapshot["items"]]


async def test_a_normal_exit_stays_buffered_until_the_turn_boundary_flush(
    loguru_records: LogRecords, live_events: LiveEvents
) -> None:
    async with _enter(live_events, _filler(5)):
        pass

    enters = _records(loguru_records, "node_enter")
    assert [(r["extra"]["node"], r["extra"]["msg_count"]) for r in enters] == [("llm", 5)]
    assert _records(loguru_records, "node_exit") == []

    node_log.flush_node_exit_aggregate(1)

    (exit_record,) = _records(loguru_records, "node_exit")
    assert exit_record["extra"]["count"] == 1
    (node,) = exit_record["extra"]["nodes"]
    assert (node["node"], node["outcome"]) == ("llm", "ok")
    assert node["duration_seconds"] >= 0
    assert exit_record["exception"] is None


async def test_a_cancelled_exit_is_buffered_as_normal_flow_not_an_exception_record(
    loguru_records: LogRecords, live_events: LiveEvents
) -> None:
    with pytest.raises(asyncio.CancelledError):
        async with _enter(live_events, _filler(5)):
            raise asyncio.CancelledError

    assert _records(loguru_records, "node_exit") == []
    node_log.flush_node_exit_aggregate(1)

    (exit_record,) = _records(loguru_records, "node_exit")
    assert [(n["node"], n["outcome"]) for n in exit_record["extra"]["nodes"]] == [
        ("llm", "cancelled")
    ]
    assert exit_record["level"].name == "INFO"


async def test_the_safety_cap_flushes_when_a_turn_never_reaches_claim(
    loguru_records: LogRecords, live_events: LiveEvents
) -> None:
    for _ in range(32):
        async with _enter(live_events, _filler(1)):
            pass

    (exit_record,) = _records(loguru_records, "node_exit")
    assert exit_record["extra"]["count"] == 32
    assert len(exit_record["extra"]["nodes"]) == 32


async def test_an_exception_exit_logs_at_once_with_the_live_traceback(
    loguru_records: LogRecords, live_events: LiveEvents
) -> None:
    with pytest.raises(RuntimeError, match="simulated exec crash"):
        async with _enter(live_events, _filler(10), node="exec"):
            raise RuntimeError("simulated exec crash")

    (exit_record,) = _records(loguru_records, "node_exit")
    assert exit_record["extra"]["outcome"] == "exception:RuntimeError"
    assert exit_record["extra"]["agent_id"] == 1
    assert exit_record["level"].name == "INFO"
    assert exit_record["exception"].type is RuntimeError
    assert "simulated exec crash" in str(exit_record["exception"].value)


async def test_every_exit_record_names_its_owning_agent(
    loguru_records: LogRecords, live_events: LiveEvents
) -> None:
    async with _enter(live_events, _filler(1)):
        pass
    with pytest.raises(RuntimeError, match="simulated exec crash"):
        async with _enter(live_events, _filler(1), node="exec"):
            raise RuntimeError("simulated exec crash")
    node_log.flush_node_exit_aggregate(1)

    exits = _records(loguru_records, "node_exit")
    assert [record["extra"]["agent_id"] for record in exits] == [1, 1]


async def test_the_enter_snapshot_carries_the_full_history_length(live_events: LiveEvents) -> None:
    async with _enter(live_events, _filler(7)):
        pass

    (snapshot,) = _snapshots(await live_events.events())
    assert snapshot["agent_id"] == 1
    assert snapshot["msg_count"] == 7


async def test_the_first_snapshot_carries_the_system_prompt_and_later_ones_only_what_is_new(
    live_events: LiveEvents,
) -> None:
    first = _history("hello")
    async with _enter(live_events, first):
        pass
    async with _enter(live_events, [*first, AIMessage(content="second")]):
        pass

    full, incremental = _snapshots(await live_events.events())
    assert full["msg_count"] == 2
    assert _item_ids(full) == ["0.0", "1.0"]
    assert full["items"][0]["kind"] == "system_prompt"
    assert incremental["msg_count"] == 3
    assert _item_ids(incremental) == ["2.0"]


async def test_an_enter_with_nothing_new_publishes_nothing(live_events: LiveEvents) -> None:
    async with _enter(live_events, _filler(7)):
        pass
    async with _enter(live_events, _filler(7)):
        pass

    assert len(_snapshots(await live_events.events())) == 1


async def test_an_empty_window_publishes_nothing_and_leaves_the_cursor_so_a_shorter_history_rerenders(
    live_events: LiveEvents,
) -> None:
    async with _enter(live_events, _history("a")):
        pass
    async with _enter(live_events, []):
        pass
    assert len(_snapshots(await live_events.events())) == 1

    async with _enter(live_events, _history("b")):
        pass
    assert len(_snapshots(await live_events.events())) == 0

    async with _enter(live_events, [SystemMessage(content="x")]):
        pass
    (rerendered,) = _snapshots(await live_events.events())
    assert rerendered["msg_count"] == 1


async def test_a_compaction_that_shrinks_the_history_forces_a_full_window(
    live_events: LiveEvents,
) -> None:
    async with _enter(live_events, _history(*(str(i) for i in range(41)))):
        pass
    async with _enter(live_events, _history("a", "b")):
        pass

    _, rerendered = _snapshots(await live_events.events())
    assert rerendered["msg_count"] == 3
    assert _item_ids(rerendered) == ["0.0", "1.0", "2.0"]


async def test_a_full_window_request_republishes_the_window_and_advances_the_cursor(
    live_events: LiveEvents,
) -> None:
    messages = _history("first")
    async with _enter(live_events, messages):
        pass
    async with _enter(live_events, messages, full_window=True):
        pass
    async with _enter(live_events, messages):
        pass

    first, forced = _snapshots(await live_events.events())
    assert _item_ids(first) == _item_ids(forced) == ["0.0", "1.0"]


async def test_the_timeline_limit_bounds_a_full_window_snapshot(live_events: LiveEvents) -> None:
    async with _enter(live_events, _history("a", "b", "c", "d"), timeline_limit=2):
        pass

    (snapshot,) = _snapshots(await live_events.events())
    assert snapshot["msg_count"] == 5
    assert _item_ids(snapshot) == ["3.0", "4.0"]


async def test_a_render_failure_leaves_the_cursor_so_the_next_enter_renders_the_full_window(
    live_events: LiveEvents,
) -> None:
    def failing_timestamps() -> bool:
        raise RuntimeError("simulated render failure")

    summary = HumanMessage(
        content="summary",
        additional_kwargs={"meiva_msg_type": MeivaMsgType.COMPACT_SUMMARY.value},
    )
    messages: list[BaseMessage] = [SystemMessage(content="x"), summary, AIMessage(content="b")]

    with pytest.raises(RuntimeError, match="simulated render failure"):
        async with _enter(
            live_events,
            messages,
            timeline_inputs=TimestampPolicy(
                clock=Clock.from_settings(), timestamps_enabled=failing_timestamps()
            ),
        ):
            pass
    assert _snapshots(await live_events.events()) == []

    async with _enter(live_events, messages):
        pass
    (snapshot,) = _snapshots(await live_events.events())
    assert _item_ids(snapshot) == ["0.0", "1.0", "2.0"]
