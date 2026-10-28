# meiva:note
# The idle-heartbeat projection: the daemon's next check-in, over plain values.

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from base.api_contracts.agent_contract import AgentStatus
from gateway.inspect._heartbeat import HeartbeatCadence, HeartbeatState, plan_heartbeat
from gateway.inspect._live import project_heartbeat

LAST_ACTIVE = datetime(2026, 1, 1, tzinfo=UTC)
CADENCE = HeartbeatCadence(
    idle_threshold_s=600, interval_s=3600, jitter_span_s=300, backoff_max_interval_s=86400
)
IDLE = HeartbeatState(
    agent_id=17,
    status=AgentStatus.IDLING,
    last_active_at=LAST_ACTIVE,
    last_heartbeat_at=None,
    paused_until=None,
    pending_inbound=False,
    backoff_level=0,
    impersonation_active=False,
)


def test_an_idle_agent_is_due_after_the_idle_threshold_plus_its_jitter() -> None:
    plan = plan_heartbeat(IDLE, CADENCE)
    assert plan.next_at == LAST_ACTIVE + timedelta(seconds=600 + 17 % 300)
    assert (plan.paused_until, plan.heartbeat_pending) == (None, False)


def test_zero_jitter_span_disables_jitter() -> None:
    plan = plan_heartbeat(IDLE, replace(CADENCE, jitter_span_s=0))
    assert plan.next_at == LAST_ACTIVE + timedelta(seconds=600)


def test_the_last_reminder_is_a_floor_for_the_next_one() -> None:
    reminded = LAST_ACTIVE + timedelta(hours=1)
    plan = plan_heartbeat(replace(IDLE, last_heartbeat_at=reminded), CADENCE)
    assert plan.next_at == reminded + timedelta(seconds=3600)


def test_an_old_reminder_does_not_pull_the_check_in_before_the_idle_clock() -> None:
    plan = plan_heartbeat(replace(IDLE, last_heartbeat_at=LAST_ACTIVE - timedelta(days=1)), CADENCE)
    assert plan.next_at == LAST_ACTIVE + timedelta(seconds=600 + 17)


def test_a_pause_hides_the_projection() -> None:
    until = LAST_ACTIVE + timedelta(hours=2)
    plan = plan_heartbeat(replace(IDLE, paused_until=until), CADENCE)
    assert (plan.next_at, plan.paused_until, plan.heartbeat_pending) == (None, until, False)


# meiva:note
# A pause is the daemon's floor on its due predicate, not an idle-only state: an agent that
# paused for a day and then kept working still has a definite next look. The 2026-10-28
# report read "—" for the whole day.
def test_a_running_agent_reports_its_active_pause() -> None:
    until = LAST_ACTIVE + timedelta(hours=6)
    plan = plan_heartbeat(replace(IDLE, status=AgentStatus.RUNNING, paused_until=until), CADENCE)
    assert (plan.next_at, plan.paused_until, plan.heartbeat_pending) == (None, until, False)


def test_a_pause_outranks_a_queued_wake_for_a_running_agent() -> None:
    until = LAST_ACTIVE + timedelta(hours=6)
    plan = plan_heartbeat(
        replace(IDLE, status=AgentStatus.RUNNING, paused_until=until, pending_inbound=True),
        CADENCE,
    )
    assert (plan.next_at, plan.paused_until, plan.heartbeat_pending) == (None, until, False)


def test_a_running_agent_without_a_pause_has_nothing_scheduled() -> None:
    plan = plan_heartbeat(replace(IDLE, status=AgentStatus.RUNNING), CADENCE)
    assert (plan.next_at, plan.paused_until, plan.heartbeat_pending) == (None, None, False)


def test_a_terminated_agent_shows_no_pause_left_on_its_row() -> None:
    until = LAST_ACTIVE + timedelta(hours=2)
    plan = plan_heartbeat(replace(IDLE, status=AgentStatus.TERMINATED, paused_until=until), CADENCE)
    assert (plan.next_at, plan.paused_until, plan.heartbeat_pending) == (None, None, False)


def test_a_queued_wake_shows_pending_instead_of_a_time() -> None:
    plan = plan_heartbeat(replace(IDLE, pending_inbound=True), CADENCE)
    assert (plan.next_at, plan.paused_until, plan.heartbeat_pending) == (None, None, True)


def test_a_pause_outranks_a_queued_wake() -> None:
    until = LAST_ACTIVE + timedelta(hours=2)
    plan = plan_heartbeat(replace(IDLE, paused_until=until, pending_inbound=True), CADENCE)
    assert (plan.paused_until, plan.heartbeat_pending) == (until, False)


def test_a_backoff_level_stretches_the_reminder_floor() -> None:
    reminded = LAST_ACTIVE + timedelta(minutes=1)
    plan = plan_heartbeat(replace(IDLE, last_heartbeat_at=reminded, backoff_level=2), CADENCE)
    assert plan.next_at == reminded + timedelta(seconds=4 * 3600)


def test_the_backoff_floor_stops_at_the_daemons_ceiling() -> None:
    reminded = LAST_ACTIVE + timedelta(minutes=1)
    plan = plan_heartbeat(replace(IDLE, last_heartbeat_at=reminded, backoff_level=9), CADENCE)
    assert plan.next_at == reminded + timedelta(hours=24)


def test_a_backoff_never_pulls_a_check_in_before_the_idle_clock() -> None:
    plan = plan_heartbeat(
        replace(IDLE, last_heartbeat_at=LAST_ACTIVE - timedelta(days=2), backoff_level=4), CADENCE
    )
    assert plan.next_at == LAST_ACTIVE + timedelta(seconds=600 + 17)


# meiva:note
# The selection SQL reads `CASE WHEN active.id IS NOT NULL THEN 0 ELSE heartbeat_backoff_level
# END`: an externally driven agent's reminder floor is never stretched, so the projection must
# not stretch it either.
def test_an_active_impersonation_lease_holds_the_backoff_at_level_zero() -> None:
    reminded = LAST_ACTIVE + timedelta(minutes=1)
    plan = plan_heartbeat(
        replace(IDLE, last_heartbeat_at=reminded, backoff_level=2, impersonation_active=True),
        CADENCE,
    )
    assert plan.next_at == reminded + timedelta(seconds=3600)


def test_a_status_outside_the_lifecycle_is_not_read_as_not_idling() -> None:
    with pytest.raises(AssertionError):
        plan_heartbeat(replace(IDLE, status=cast(AgentStatus, "paused")), CADENCE)


def test_the_inspector_block_carries_the_interval_and_the_last_pause() -> None:
    info = project_heartbeat(IDLE, CADENCE, None)
    assert info.interval_s == 3600
    assert info.next_at == plan_heartbeat(IDLE, CADENCE).next_at
    assert info.last_pause is None
