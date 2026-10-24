from datetime import UTC, datetime

import pytest

from base.agents.messages.envelope import wrap_inbound
from base.agents.messages.timestamp_policy import TimestampPolicy, TimestampSource
from base.clock import Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers


def test_one_source_mints_fresh_policies_without_changing_a_previous_units_instant() -> None:
    timers = ManualTimers(wall_start=datetime(2026, 10, 10, 12, tzinfo=UTC))
    source = TimestampSource(
        clock=Clock(ClockConfig("Asia/Tokyo", "Asia/Tokyo", False), timers=timers, now=timers.now),
        timestamps_enabled=True,
    )
    first = source.policy()
    timers.advance(60)
    second = source.policy()

    assert first.now_stamp() == "[2026-10-10 21:00:00]"
    assert second.now_stamp() == "[2026-10-10 21:01:00]"
    assert wrap_inbound("body", "agent:7", inputs=first) == (
        "Agent 7 [2026-10-10 21:00:00]:\n\nbody"
    )


def test_an_explicitly_off_policy_needs_no_instant_until_a_note_asks_for_one() -> None:
    policy = TimestampPolicy.off()
    assert wrap_inbound("body", "agent:7", inputs=policy) == "Agent 7:\n\nbody"
    with pytest.raises(RuntimeError, match="no instant"):
        policy.require_now()
