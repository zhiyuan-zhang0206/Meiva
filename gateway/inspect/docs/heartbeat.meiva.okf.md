---
type: doc
title: Inspector heartbeat projection
description: The heartbeat display states the live inspector reports, the daemon predicate each one mirrors, and how an impersonation lease moves the idle clock.
tags:
- gateway
- observability
---

# Inspector heartbeat projection

The live inspector reports an active pause — for a running agent too, because the
daemon's pause floor is not idle-only — a recent pending inbound, or the check-in
estimate from the idle clock, the idle threshold and jitter, and the reminder floor.
That floor is the last heartbeat plus the interval stretched by the agent's persisted
backoff level and capped at `MEIVA_HEARTBEAT_BACKOFF_MAX_INTERVAL_SECONDS` (a day by
default), held at level 0 while an impersonation lease drives the agent — the same value
the selection SQL binds. Under an active lease the idle clock is the lease's own start
(`COALESCE(activated_at, created_at)`, the term the selection SQL reads first), because an
externally driven agent keeps running between takeover turns instead of returning to
idling, so its `last_active_at` would never time the next check-in.
The projection itself is the strict `gateway/inspect/_heartbeat.py` (`plan_heartbeat`): the daemon's cadence
(`HeartbeatCadence` — the `daemon.heartbeat_*` settings plus the dispatch constants the route
takes from `services.wake.heartbeat`) and the agent's rows come in as values.
Its idle fixtures backdate both `status_changed_at` and `last_active_at`:
updating a timestamp alone does not invoke the status-change trigger.

## Heartbeat block states

`HeartbeatInfo` reports one of these mutually-exclusive states, and each one answers
"when does the heartbeat look at this agent again":

- **paused** (idling or running) — `paused_until` is the active suppression end and
  `next_at` is null. The pause is the daemon's unconditional floor, not an idle-only
  state, so it stays the answer while the agent is still working, and it outranks a
  lease as well as a queued wake.
- **a fresh wake already queued** — `heartbeat_pending`: the daemon suppresses check-ins
  while a fresh inbound is pending (its `NOT EXISTS` guard, windowed by
  `STALE_PENDING_S`), so no future check-in is projected. A pending inbound older than
  that window is stale, and the panel projects `next_at` instead. This is a state for any
  agent the daemon can select, an impersonated one included.
- **nothing queued** — `next_at` is the later of
  `idle clock + idle_threshold + (id mod JITTER_SPAN_S)` and, when a prior check-in
  exists, `last_heartbeat_at + min(interval_s * 2^level, configured ceiling)`, where `level`
  is the persisted backoff level unless a lease drives the agent. The idle clock is the
  lease start while an active lease drives the agent and `last_active_at` otherwise, so
  this state covers an idling agent and a running agent under an impersonation lease. The
  daemon dispatches at its first poll tick at/after that (at most one 15s dispatch step
  later), so `next_at` is the earliest possible check-in and never later than what the
  daemon does; an overdue one renders "due" in the panel.
- **running without a lease, or terminated** — nothing is projected: the daemon selects a
  busy agent only while a lease drives it (`status='running' AND active.id IS NOT NULL`),
  and the idle clock that would time a native agent's next check-in keeps moving with the
  turn; a dead agent never gets one and a pause left on its row is inert.

`interval_s` is the cluster heartbeat interval in seconds (context for `next_at`), and
`last_pause` is the most recent pause from history, independent of the current state.

The value is projected from the frame the read saw. A lease that expires before the
projected instant does not retroactively invalidate it: the daemon re-judges every tick
and the panel re-reads on its next refresh, which then finds the agent idling (projected
from `last_active_at`) or under a fresh lease. A lease already past `expires_at` drives
nothing in the read — the subquery mirrors the daemon's `expires_at > now()` join — so a
running agent whose takeover ended shows nothing until its status moves. What the
projection does not model is the daemon's separate `NOT EXISTS` gates — a lease still owing
its handoff receipt, a queued delta — which suppress selection outright in transient handoff
states rather than move the clock; the cell answers when the next check-in is due, not that
the agent is eligible this instant.
