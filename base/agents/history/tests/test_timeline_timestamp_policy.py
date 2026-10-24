from datetime import UTC, datetime

from langchain_core.messages import HumanMessage

from base.agents.history.timeline import build_timeline_items, timeline_default_limit
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig

_INSTANT = datetime(2026, 10, 4, 12, tzinfo=UTC)
_CLOCK = Clock(ClockConfig("UTC", "UTC", False), now=lambda: _INSTANT, timers=SYSTEM_TIMERS)


def test_each_renderer_follows_its_own_policy() -> None:
    message = HumanMessage(
        content="summary", additional_kwargs={"meiva_msg_type": "compact_summary"}
    )
    stamped = TimestampPolicy(clock=_CLOCK, timestamps_enabled=True)
    bare = TimestampPolicy(clock=_CLOCK, timestamps_enabled=False)

    rendered, _ = build_timeline_items([message], inputs=stamped)
    assert rendered[0].payload == "Compact summary [2026-10-04 12:00:00]:\n\nsummary"
    rendered, _ = build_timeline_items([message], inputs=bare)
    assert rendered[0].payload == "Compact summary:\n\nsummary"


def test_plain_messages_carry_no_timestamp_and_limit_is_live() -> None:
    items, count = build_timeline_items(
        [HumanMessage(content="plain")],
        inputs=TimestampPolicy(clock=_CLOCK, timestamps_enabled=True),
    )
    assert count == 1 and items[0].payload == "plain"
    limit = 7
    assert timeline_default_limit(limit_reader=lambda: limit) == 7
    limit = 11
    assert timeline_default_limit(limit_reader=lambda: limit) == 11
