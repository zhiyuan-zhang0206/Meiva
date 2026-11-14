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


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("weekday", [False, True])
def test_system_and_peer_inputs_obey_the_same_timestamp_policy(
    enabled: bool, weekday: bool
) -> None:
    now = datetime(2026, 11, 14, 8, 5, 51, tzinfo=UTC)
    timers = ManualTimers(wall_start=now)
    policy = TimestampPolicy.from_clock(
        Clock(
            ClockConfig("Asia/Shanghai", "Asia/Shanghai", weekday), timers=timers, now=timers.now
        ),
        timestamps_enabled=enabled,
    )
    stamp = "[2026-11-14 Sat 16:05:51]" if weekday else "[2026-11-14 16:05:51]"
    prefix = f"{stamp} " if enabled else ""
    assert policy.prefix(now) == prefix
    assert wrap_inbound("body", "system", inputs=policy, created_at=now) == (
        f"<system_note>\n{prefix}body\n</system_note>"
    )
    assert wrap_inbound("body", "agent:7", inputs=policy, created_at=now) == (
        f"Agent 7 {stamp}:\n\nbody" if enabled else "Agent 7:\n\nbody"
    )


def test_disabled_prefix_does_not_require_an_instant() -> None:
    assert TimestampPolicy.off().prefix() == ""


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "source",
    [
        "user",
        "agent:7",
        "external_agent:probe",
        "system",
        "system:maintenance",
        "schedule:1",
        "watcher:2",
        "shell:3",
    ],
)
def test_every_meiva_inbox_source_keeps_an_exact_body_boundary(enabled: bool, source: str) -> None:
    from base.agents.messages.envelope import inbound_body, inbound_head

    at = datetime(2026, 11, 14, 8, 5, 51, tzinfo=UTC)
    inputs = TimestampPolicy(enabled, "Asia/Shanghai", True, at)
    body = "[2026-11-14 Sat 16:05:51] literal first paragraph\n\nSecond paragraph <system_note>literal</system_note>."
    head = inbound_head(source, inputs=inputs, created_at=at)
    wrapped = wrap_inbound(body, source, inputs=inputs, created_at=at)
    assert ("[2026-11-14 Sat 16:05:51]" in head) is enabled
    assert inbound_body(wrapped, source=source, body_start=len(head)) == body
