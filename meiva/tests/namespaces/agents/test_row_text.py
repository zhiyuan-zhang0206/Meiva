from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial

import pytest

from base.agents.context import MeivaContext
from base.api_contracts.agent_contract import AgentStatus
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from meiva.agents import AgentRow, OpenTaskRow
from meiva.sdk_surface import process_context

_NOW = datetime(2026, 1, 10, 12, tzinfo=UTC)


@pytest.fixture
def row_context(sdk_environment: MeivaContext) -> Iterator[MeivaContext]:
    config = ClockConfig(
        timezone="America/Los_Angeles",
        authoritative_timezone="America/Los_Angeles",
        message_timestamp_weekday=False,
    )
    context = MeivaContext(clock_factory=partial(Clock, config, timers=SYSTEM_TIMERS))
    prior = process_context.unbind_context()
    process_context.bind_context(context)
    try:
        yield context
    finally:
        process_context.unbind_context()
        context.clients.close()
        if prior is not None:
            process_context.bind_context(prior)


def _agent_row(spawned_at: datetime, last_active_at: datetime, label: str | None) -> AgentRow:
    return AgentRow(
        agent_id=17,
        label=label,
        status=AgentStatus.RUNNING,
        spawner="user",
        fork_source_agent_id=None,
        machine="m1",
        spawned_at=spawned_at,
        last_active_at=last_active_at,
        last_inbound_at=last_active_at,
        heartbeat_paused_until=None,
    )


def test_actual_rows_keep_their_fields_and_optional_activity(row_context: MeivaContext) -> None:
    now = datetime.now(UTC)
    spawned = now - timedelta(days=1000, hours=12)
    active = now - timedelta(days=1, hours=12)
    assert str(_agent_row(spawned, active, "worker")) == (
        "#17 worker  running  machine=m1  spawned=1000d ago  last_active=1d ago"
    )
    assert str(_agent_row(spawned, spawned, None)) == "#17  running  machine=m1  spawned=1000d ago"
    assert str(OpenTaskRow(23, "fix retries", "in_progress", active)) == (
        "#23 | fix retries | in_progress | 1d ago"
    )


# meiva:note
# Choose an actual past instant across this real clock's offset change, half an offset away
# from a day boundary. Wall-time subtraction and UTC elapsed then differ by one displayed day.
@pytest.mark.parametrize("kind", ["agent", "task"])
def test_actual_dto_wiring_uses_elapsed_days_across_a_zone_change(
    row_context: MeivaContext, kind: str
) -> None:
    now = datetime.now(UTC)
    zone = row_context.require_clock().explicit_zone()
    for days in range(30, 361, 30):
        candidate = now - timedelta(days=days)
        offset = now.astimezone(zone).utcoffset()
        past_offset = candidate.astimezone(zone).utcoffset()
        assert offset is not None and past_offset is not None
        if offset != past_offset:
            moment = candidate + (offset - past_offset) / 2
            if moment.astimezone(zone).utcoffset() != offset:
                break
    else:
        raise AssertionError("Los Angeles must have an offset change within the past year")
    expected = f"{(now - moment).days}d ago"
    row = (
        _agent_row(moment, moment, None)
        if kind == "agent"
        else OpenTaskRow(23, "task", "open", moment)
    )
    assert str(row).endswith(expected)


def test_dto_naive_input_keeps_the_facade_conversion(row_context: MeivaContext) -> None:
    now = datetime.now(UTC)
    moment = (now - timedelta(days=10, hours=12, minutes=7)).replace(tzinfo=None)
    converted = moment.astimezone(row_context.require_clock().zone())
    days = (now - converted.astimezone(UTC)).days
    assert str(OpenTaskRow(23, "task", "open", moment)) == f"#23 | task | open | {days}d ago"


def test_actual_rows_still_require_the_bound_context_clock(row_context: MeivaContext) -> None:
    process_context.bind_context(replace(row_context, clock_factory=None))
    try:
        for row in (_agent_row(_NOW, _NOW, None), OpenTaskRow(23, "task", "open", _NOW)):
            with pytest.raises(RuntimeError, match="clock"):
                str(row)
    finally:
        process_context.bind_context(row_context)
