import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from zoneinfo import ZoneInfoNotFoundError

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from agent.graph import node_log
from agent.graph.node_log import StallConfig, node_lifecycle
from base.agents.epochs.native_slice import NativeEpochSlice
from base.agents.messages.kwargs import MeivaMsgType
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.agents.observation.turn_progress import TurnProgress
from base.clock import Clock
from base.clock.tests.fakes import clock_with_unknown_timezone
from base.events.live.tests.live_capture import LiveEvents
from base.host.boundary.fields import (
    optional_float_field,
    optional_list_field,
    str_field,
)
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_mapping, object_of
from base.log.tests.log_records import LogRecords

_TIMELINE_INPUTS = TimestampPolicy.from_clock(Clock.from_settings(), timestamps_enabled=False)
_TIMELINE_LIMIT = 100


def _enter(
    live: LiveEvents,
    messages: list[BaseMessage],
    *,
    node: str = "llm",
    full_window: bool = False,
    timeline_inputs: TimestampPolicy = _TIMELINE_INPUTS,
    timeline_limit: int = _TIMELINE_LIMIT,
    epoch_slice: NativeEpochSlice | None = None,
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
        epoch_slice=epoch_slice,
    )


def _filler(count: int) -> list[BaseMessage]:
    return [SystemMessage(content="x")] * count


def _history(*texts: str) -> list[BaseMessage]:
    return [SystemMessage(content="prompt"), *(AIMessage(content=text) for text in texts)]


def _records(records: LogRecords, event: str) -> list[dict[str, object]]:
    return records.extras(event=event)


def _exit_nodes(exit_record: Mapping[str, object]) -> list[dict[str, object]]:
    nodes = optional_list_field(exit_record, "nodes")
    assert nodes is not None
    entries = [node for node in nodes if is_object_dict(node)]
    assert len(entries) == len(nodes)
    return entries


def _exit_exception(records: LogRecords) -> object:
    (record,) = records.events("node_exit")
    return object_mapping(record)["exception"]


def _snapshots(events: list[dict[str, object]]) -> list[dict[str, object]]:
    return [event for event in events if event["role"] == "timeline_snapshot"]


def _snapshot_items(snapshot: Mapping[str, object]) -> list[dict[str, object]]:
    items = optional_list_field(snapshot, "items")
    assert items is not None
    entries = [item for item in items if is_object_dict(item)]
    assert len(entries) == len(items)
    return entries


def _item_ids(snapshot: Mapping[str, object]) -> list[str]:
    return [str_field(item, "item_id") for item in _snapshot_items(snapshot)]


async def test_a_normal_exit_stays_buffered_until_the_turn_boundary_flush(
    loguru_records: LogRecords, live_events: LiveEvents
) -> None:
    async with _enter(live_events, _filler(5)):
        pass

    enters = _records(loguru_records, "node_enter")
    assert [(r["node"], r["msg_count"]) for r in enters] == [("llm", 5)]
    assert _records(loguru_records, "node_exit") == []

    node_log.flush_node_exit_aggregate(1)

    (exit_record,) = _records(loguru_records, "node_exit")
    assert exit_record["count"] == 1
    (node,) = _exit_nodes(exit_record)
    assert (node["node"], node["outcome"]) == ("llm", "ok")
    duration = optional_float_field(node, "duration_seconds")
    assert duration is not None and duration >= 0
    assert _exit_exception(loguru_records) is None


async def test_a_cancelled_exit_is_buffered_as_normal_flow_not_an_exception_record(
    loguru_records: LogRecords, live_events: LiveEvents
) -> None:
    with pytest.raises(asyncio.CancelledError):
        async with _enter(live_events, _filler(5)):
            raise asyncio.CancelledError

    assert _records(loguru_records, "node_exit") == []
    node_log.flush_node_exit_aggregate(1)

    (exit_record,) = _records(loguru_records, "node_exit")
    assert [(n["node"], n["outcome"]) for n in _exit_nodes(exit_record)] == [("llm", "cancelled")]
    assert loguru_records.extras(event="node_exit", level="INFO") == [exit_record]


async def test_the_safety_cap_flushes_when_a_turn_never_reaches_claim(
    loguru_records: LogRecords, live_events: LiveEvents
) -> None:
    for _ in range(32):
        async with _enter(live_events, _filler(1)):
            pass

    (exit_record,) = _records(loguru_records, "node_exit")
    assert exit_record["count"] == 32
    assert len(_exit_nodes(exit_record)) == 32


async def test_an_exception_exit_logs_at_once_with_the_live_traceback(
    loguru_records: LogRecords, live_events: LiveEvents
) -> None:
    with pytest.raises(RuntimeError, match="simulated exec crash"):
        async with _enter(live_events, _filler(10), node="exec"):
            raise RuntimeError("simulated exec crash")

    (exit_record,) = _records(loguru_records, "node_exit")
    assert exit_record["outcome"] == "exception:RuntimeError"
    assert exit_record["agent_id"] == 1
    assert loguru_records.extras(event="node_exit", level="INFO") == [exit_record]
    exception = _exit_exception(loguru_records)
    assert object_of(getattr, exception, "type") is RuntimeError
    value = object_of(getattr, exception, "value")
    assert isinstance(value, RuntimeError)
    assert "simulated exec crash" in str(value)


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
    assert [record["agent_id"] for record in exits] == [1, 1]


async def test_the_enter_snapshot_carries_the_full_history_length(live_events: LiveEvents) -> None:
    async with _enter(live_events, _filler(7)):
        pass

    (snapshot,) = _snapshots(await live_events.object_events())
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

    full, incremental = _snapshots(await live_events.object_events())
    assert full["msg_count"] == 2
    assert _item_ids(full) == ["0.0", "1.0"]
    assert _snapshot_items(full)[0]["kind"] == "system_prompt"
    assert incremental["msg_count"] == 3
    assert _item_ids(incremental) == ["2.0"]


async def test_an_enter_with_nothing_new_publishes_nothing(live_events: LiveEvents) -> None:
    async with _enter(live_events, _filler(7)):
        pass
    async with _enter(live_events, _filler(7)):
        pass

    assert len(_snapshots(await live_events.object_events())) == 1


async def test_an_empty_window_publishes_nothing_and_leaves_the_cursor_so_a_shorter_history_rerenders(
    live_events: LiveEvents,
) -> None:
    async with _enter(live_events, _history("a")):
        pass
    async with _enter(live_events, []):
        pass
    assert len(_snapshots(await live_events.object_events())) == 1

    async with _enter(live_events, _history("b")):
        pass
    assert len(_snapshots(await live_events.object_events())) == 0

    async with _enter(live_events, [SystemMessage(content="x")]):
        pass
    (rerendered,) = _snapshots(await live_events.object_events())
    assert rerendered["msg_count"] == 1


async def test_a_compaction_that_shrinks_the_history_forces_a_full_window(
    live_events: LiveEvents,
) -> None:
    async with _enter(live_events, _history(*(str(i) for i in range(41)))):
        pass
    async with _enter(live_events, _history("a", "b")):
        pass

    _, rerendered = _snapshots(await live_events.object_events())
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

    first, forced = _snapshots(await live_events.object_events())
    assert _item_ids(first) == _item_ids(forced) == ["0.0", "1.0"]


async def test_the_timeline_limit_bounds_a_full_window_snapshot(live_events: LiveEvents) -> None:
    async with _enter(live_events, _history("a", "b", "c", "d"), timeline_limit=2):
        pass

    (snapshot,) = _snapshots(await live_events.object_events())
    assert snapshot["msg_count"] == 5
    assert _item_ids(snapshot) == ["3.0", "4.0"]


async def test_a_full_window_holds_only_the_items_of_the_current_epoch(
    live_events: LiveEvents,
) -> None:
    def reply(text: str, hour: int) -> AIMessage:
        stamp = f"2026-11-25T{hour:02d}:30:00+00:00"
        return AIMessage(content=text, additional_kwargs={"meiva_created_at": stamp})

    history: list[BaseMessage] = [
        SystemMessage(content="prompt"),
        reply("before the switch", 2),
        reply("after the switch", 6),
    ]
    switched = datetime(2026, 11, 25, 5, tzinfo=UTC)
    async with _enter(
        live_events, history, epoch_slice=NativeEpochSlice(switched, None, first=False)
    ):
        pass

    (snapshot,) = _snapshots(await live_events.object_events())
    assert _item_ids(snapshot) == ["2.0"]
    assert snapshot["msg_count"] == 3


async def test_a_render_failure_leaves_the_cursor_so_the_next_enter_renders_the_full_window(
    live_events: LiveEvents,
) -> None:
    summary = HumanMessage(
        content="summary",
        additional_kwargs={"meiva_msg_type": MeivaMsgType.COMPACT_SUMMARY.value},
    )
    messages: list[BaseMessage] = [SystemMessage(content="x"), summary, AIMessage(content="b")]

    with pytest.raises(ZoneInfoNotFoundError):
        async with _enter(
            live_events,
            messages,
            timeline_inputs=TimestampPolicy.from_clock(
                clock_with_unknown_timezone(), timestamps_enabled=True
            ),
        ):
            pass
    assert _snapshots(await live_events.object_events()) == []

    async with _enter(live_events, messages):
        pass
    (snapshot,) = _snapshots(await live_events.object_events())
    assert _item_ids(snapshot) == ["0.0", "1.0", "2.0"]
