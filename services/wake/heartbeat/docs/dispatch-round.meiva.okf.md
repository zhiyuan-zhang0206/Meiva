---
type: doc
title: Heartbeat dispatch rounds
description: Required round and clock inputs preserve heartbeat pacing, backoff, failure and cancellation boundaries.
tags:
- heartbeat
- lifecycle
---

# Heartbeat dispatch rounds

`HeartbeatDispatch` owns one daemon generation's pending check-in readings,
failure streaks, no-op streaks and deadline state. A round sweeps persisted
backoff resets, reconciles previous outcomes, selects due agents and sends each
check-in. An individual send failure is logged and does not prevent other
selected agents from receiving their check-ins; only a successful send records
that agent's pending idle reading. A daemon restart starts these four maps
empty, so it probes agents at normal cadence. The persisted B7 backoff level
remains in PostgreSQL.

The dispatch cadence and idle threshold are captured once, matching the old
loop startup reads. The no-op-nudge threshold stays a live read in each call to
`reconcile_checkin_outcomes`; it is not frozen with startup configuration.
Selection applies the absolute backoff filter before the per-step limit, so a
backed-off agent cannot consume a healthy agent's wake-rate slot. The shared
jitter, 15-second dispatch ceiling and 25-check-in cap continue to flatten fleet
wake storms.

## Round pacing and failure

`loops.dispatch_loop` takes an owned synchronous round. It waits before its
first round. `loops.liveness_loop` takes an owned asynchronous liveness round;
it starts immediately and waits between passes, including after a failed pass.
The production liveness round still performs the existing database/host merge.
Each loop owns its existing consecutive-failure budget: a completed round
clears the streak; a database operational outage does not consume it; repeated
unexpected errors eventually end the process. Dispatch schema/syntax errors
remain immediately fatal. Cancellation propagates through either loop.
These rules differ from the generic first-unexpected-error `run_rounds` policy.

Waiting uses a required `Clock`, beating liveness before each at-most-30-second
sleep, so long healthy waits remain visible. Backoff deadlines and selection
read `Clock.now().timestamp()` from the same owner, retaining their original
absolute wall-clock axis. PostgreSQL idle readings still use the database clock.
The clock's timer port controls waits without changing those data semantics.

## Composition and tests

The entry's real TaskGroup starts dispatch, liveness and completion digest with
separate progress trackers. A resident-loop failure still cancels its siblings;
health-before-pool ordering and pool/health/pidfile cleanup remain with the
existing daemon root. That composition is separate from a dispatch round's
business collaborators; no default callback bag selects a second implementation.

Failure-budget tests supply complete scripted rounds and manual timers rather
than patching six query/send/sleep globals. Retry coverage uses the actual
selection and reconciliation against PostgreSQL with one recorded send method,
and a shared manual wall clock drives both deadline assignment and filtering.
Liveness ordering tests execute the actual paced wait and record its 30-second
chunks. TaskGroup composition tests retain actual sibling cancellation, image,
progress tracker, pool/health cleanup and pidfile assertions.
