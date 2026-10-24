from datetime import UTC, datetime

import pytest

from base.agents.messages.envelope import inbound_head, wrap_inbound
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig

_INSTANT = datetime(2026, 10, 4, 12, tzinfo=UTC)
_CLOCK = Clock(ClockConfig("UTC", "UTC", False), now=lambda: _INSTANT, timers=SYSTEM_TIMERS)


def test_each_envelope_follows_its_own_policy() -> None:
    stamped = TimestampPolicy.from_clock(_CLOCK, timestamps_enabled=True)
    bare = TimestampPolicy.from_clock(_CLOCK, timestamps_enabled=False)
    assert wrap_inbound("body", "agent:7", inputs=stamped) == (
        "Agent 7 [2026-10-04 12:00:00]:\n\nbody"
    )
    assert wrap_inbound("body", "agent:7", inputs=bare) == "Agent 7:\n\nbody"
    assert inbound_head("user", inputs=bare) == ""
    assert inbound_head("user", inputs=stamped, created_at=_INSTANT) == "[2026-10-04 12:00:00]\n\n"


def test_system_header_carries_no_stamp() -> None:
    inputs = TimestampPolicy.from_clock(_CLOCK, timestamps_enabled=True)
    assert inbound_head("system:restart", inputs=inputs) == "<system_note>\n"


def test_external_agent_header_names_the_sender_and_follows_the_timestamp_policy() -> None:
    instant = datetime(2026, 10, 4, 12, tzinfo=UTC)
    clock = Clock(ClockConfig("UTC", "UTC", False), now=lambda: instant, timers=SYSTEM_TIMERS)
    stamped = TimestampPolicy.from_clock(clock, timestamps_enabled=True)
    bare = TimestampPolicy.from_clock(clock, timestamps_enabled=False)
    source = "external_agent:codex:run-42"
    assert inbound_head(source, inputs=stamped, created_at=instant) == (
        "External agent (codex / run-42; asserted provenance) [2026-10-04 12:00:00]:\n\n"
    )
    assert (
        inbound_head(source, inputs=bare)
        == "External agent (codex / run-42; asserted provenance):\n\n"
    )
    assert inbound_head("external_agent:claude_code", inputs=bare) == (
        "External agent (claude_code; asserted provenance):\n\n"
    )


def test_unknown_source_is_refused_whatever_the_policy() -> None:
    with pytest.raises(ValueError, match="Unrecognized inbound source"):
        inbound_head("unknown", inputs=TimestampPolicy.from_clock(_CLOCK, timestamps_enabled=False))
