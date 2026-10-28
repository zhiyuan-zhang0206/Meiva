# meiva:note
# When the idle-heartbeat daemon checks in on one agent next: values in, a projection out.
#
# The inspector panel shows the daemon's decision before the daemon makes it, so the due time here
# mirrors the daemon's predicate exactly. Nothing is read from configuration or the clock: the
# cadence the daemon runs with and the agent's current rows come in as values, and a test passes
# the instants it wants.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import assert_never

from base.api_contracts.agent_contract import AgentStatus

__all__ = ["HeartbeatCadence", "HeartbeatPlan", "HeartbeatState", "plan_heartbeat"]


# meiva:note
# The daemon's timing: idle threshold, reminder interval, the per-agent jitter span, and the
# ceiling on the backoff-stretched reminder floor.
@dataclass(frozen=True)
class HeartbeatCadence:
    idle_threshold_s: int
    interval_s: int
    jitter_span_s: int
    backoff_max_interval_s: int


# meiva:note
# One agent's current rows, as the request's fresh agents_meta projection read them.
#
# `paused_until` is already narrowed to a *future* value (a NULL or past pause is None).
# `pending_inbound` is true only for a FRESH pending inbound (created within the daemon's
# freshness window). `backoff_level` is the agent's persisted nudge-backoff level and
# `impersonation_active` says whether a live impersonation lease drives it; both feed the
# daemon's reminder floor.
@dataclass(frozen=True)
class HeartbeatState:
    agent_id: int
    status: AgentStatus
    last_active_at: datetime
    last_heartbeat_at: datetime | None
    paused_until: datetime | None
    pending_inbound: bool
    backoff_level: int
    impersonation_active: bool


# meiva:note
# The display state, mutually exclusive: a pause, a queued wake, a due time, or nothing.
@dataclass(frozen=True)
class HeartbeatPlan:
    next_at: datetime | None
    paused_until: datetime | None
    heartbeat_pending: bool


# meiva:note
# No check-in is scheduled for this agent: its turn is running, or it is dead.
_NO_CHECK_IN = HeartbeatPlan(next_at=None, paused_until=None, heartbeat_pending=False)


# meiva:note
# Idle-heartbeat state for one agent — the projected next check-in (or the active pause /
# already-queued wake).
#
# - paused (running or idling) → `paused_until`.
# - idling, not paused, a wake already queued → `heartbeat_pending`.
# - idling, not paused, nothing queued → `next_at` (the daemon's projected check-in due time —
#   the later of the idle clock plus its per-agent jitter and the durable last-reminder cadence
#   floor, that floor being the interval stretched by the agent's backoff level; the daemon
#   dispatches at its first poll tick at/after that, at most one 15s dispatch step later. The
#   frontend renders an overdue one as "due").
# - running, not paused → all off: a busy agent gets no check-in, and the idle clock that would
#   time the next one keeps moving with it, so any projected instant would be stale.
# - terminated → all off: a dead agent never gets a check-in, and a pause left on its row is
#   inert rather than a wake still to come.
#
# The pause is the daemon's unconditional floor on its due predicate — the selection SQL reads
# `now() >= GREATEST(heartbeat_paused_until, ...)` outside its status gate — so it answers "when
# does the heartbeat look at this agent again" for a running agent too. Reading it only while
# idling hid a day-long pause behind "—" for as long as the agent kept working (user report
# 2026-10-28).
#
# `heartbeat_pending` mirrors the daemon's `NOT EXISTS (pending inbound)` guard in full —
# including its freshness window: only a *fresh* pending inbound (a heartbeat check-in already
# sent but not yet processed, or a chat about to wake it) suppresses the daemon's next
# check-in. A pending inbound older than that window no longer counts as "about to wake" — the
# daemon checks in on the agent anyway. Surfacing this state — instead of projecting `next_at`
# off a stale clock — is what keeps a stuck agent from rendering a nonsensical past "next
# heartbeat" time. When no inbound is pending, any earlier check-in was already consumed,
# `last_heartbeat_at` remains the durable cadence floor even when its turn did not produce
# any LLM work.
def plan_heartbeat(state: HeartbeatState, cadence: HeartbeatCadence) -> HeartbeatPlan:
    match state.status:
        case AgentStatus.IDLING | AgentStatus.RUNNING:
            pass
        case AgentStatus.TERMINATED:
            return _NO_CHECK_IN
        case _:
            assert_never(state.status)
    if state.paused_until is not None:
        return HeartbeatPlan(next_at=None, paused_until=state.paused_until, heartbeat_pending=False)
    if state.status == AgentStatus.RUNNING:
        return _NO_CHECK_IN
    if state.pending_inbound:
        return HeartbeatPlan(next_at=None, paused_until=None, heartbeat_pending=True)
    # meiva:note
    # Match the daemon's due-time exactly: the later of the idle clock plus per-agent jitter and
    # the durable reminder floor. The jitter span is whole seconds so this matches the daemon's
    # `NULLIF(span, 0)::int` cast; the `0` guard mirrors the daemon's disabled-jitter collapse
    # (mod never divides by zero).
    #
    # The floor is the interval stretched by the agent's persisted backoff level and capped at
    # the daemon's 24h ceiling — held at level 0 while an impersonation lease is active, because
    # the selection SQL's `CASE WHEN active.id IS NOT NULL THEN 0` does not stretch it for an
    # externally driven agent.
    span_s = cadence.jitter_span_s
    jitter_s = state.agent_id % span_s if span_s else 0
    idle_due_at = state.last_active_at + timedelta(seconds=cadence.idle_threshold_s + jitter_s)
    level = 0 if state.impersonation_active else state.backoff_level
    reminder_interval_s = min(cadence.interval_s * 2**level, cadence.backoff_max_interval_s)
    reminder_due_at = (
        state.last_heartbeat_at + timedelta(seconds=reminder_interval_s)
        if state.last_heartbeat_at is not None
        else idle_due_at
    )
    return HeartbeatPlan(
        next_at=max(idle_due_at, reminder_due_at), paused_until=None, heartbeat_pending=False
    )
