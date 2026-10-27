# 0012 — A stop that removes the message plane needs an outside rescuer

**Date:** 2026-09-17
**Anchors:** `gateway/cluster/server.py` (`serve_kwargs`, `timeout_graceful_shutdown`),
`base/config/domains/gateway.py` (`gateway_graceful_shutdown_timeout_seconds`),
`gateway/tests/bootstrap/test_server_shutdown.py`; fixes: #2712 (commit `7187029404`),
#2710 (commit `161f9d1b3`, the convention amendment written the same day). The
drill's own records — the executor's logs, the stopped home's lifecycle journal and
hold, the out-of-band probe's evidence — are not in this repo `(summarized)`; the
times below come from them and from the incident record assembled that afternoon.
Written on 2026-10-28 from that record; later changes are dated and linked where they
matter.

## Summary

A drill leg stopped the message plane to exercise recovery. The gateway would not
exit on SIGTERM: uvicorn's connection-drain phase had no budget, and the gateway's
long-lived client streams never close on their own, so two attempts spent 444 s
failing and only `--force` ended the process, eight minutes in. That force stop was
the last step its executor — an agent turn — completed: seconds later the delivery
watchdog cancelled the turn, and everything after the stop (the convergence checks
and the start that ends the window) had been steps of that turn. The stop retained an
explicit hold with no expiry, by design: nothing may restart a stack an operator
deliberately took down. With the operator gone, nothing owned that hold and nothing
watched it — a mechanism deleted on 2026-10-20, so this particular permanence is not a
live one. The fleet stayed dark for 110 minutes until a human ran `start` from outside
the cluster. One out-of-band probe had detected the outage
at 08:47:54 and delivered its notice; by then nothing was left that could act on it.

The rule: an operation that takes the message plane down cannot be reported, observed
or rescued through it — including by its own executor. Name the rescuer, the record
and the silence bound outside the plane before the first stop leg.

## Timeline

Beijing local. The leg's window opened at 08:30 with the services and the data plane
as its target; its own steps ended with a restart.

- 08:34:55 — the executor's operation log takes its last line. Everything the
  operation knew after this point existed only inside one turn.
- 08:42:13 — the stop is issued.
- 08:42:20 — the gateway logs the drain it has entered: `Waiting for connections to
  close`. Its listener is already closed; from 08:42:52 every new connection is
  refused (the out-of-band probe's first observation).
- 08:47:14 — the first stop attempt returns `rc=1` after 294 s: every unit stopped
  except the gateway.
- 08:47:54 — the out-of-band probe crosses its threshold (six consecutive refusals,
  302 s after the first) and its alert is accepted by the platform.
- 08:50:39 — the executor tries to send a message and cannot: the plane it reports
  through is the plane it stopped.
- 08:51:22 — a second stop attempt; `rc=1` at 08:53:53 after 149.9 s.
- 08:54:30–42 — a third attempt with force returns `rc=0` in 12 s. Everything is down.
- ~08:54:2x–4x — the delivery watchdog cancels the executor's turn, which had been
  inside one tool call for eight minutes. The convergence checks and the restart that
  ends the window never run.
- 08:54–10:44 — nothing owns the stopped home. One downstream line loses 153 wake
  deliveries between 09:04 and 10:34, its last success having been 08:40:16; another
  loses one-shot results it has to redo.
- 10:44:23–26 — services start from outside the cluster, in a session that outlived
  the outage: new gateway, pooler and Redis processes, the VM never rebooted (up
  1d7h), the checkout's SHA unchanged.
- 10:45:10, 10:46:11 — the probe's recovery notices. 10:48:10 — the first message
  delivered after recovery.

Impact: 110 minutes in which every agent on the stopped home was silent and every
wake path crossed a stopped plane. No data loss and no restart of the machine; the
cost was the window itself plus the re-run of every monitor whose one-shot result the
outage swallowed.

## Root cause

### The trigger: an unbounded connection drain

`uvicorn.run` was called from `main()` with the launch parameters assembled inline,
and `timeout_graceful_shutdown` was not among them. Its default is `None`: after
SIGTERM, uvicorn closes the listener and then waits, without a bound, for every open
connection to finish. The gateway's client channel is a long-lived stream held open
for hours, with no reason to end at shutdown, so the drain waited on connections that
would not close. Which connection did it was never pinned afterwards; the class was,
and one stuck stream reproduces it. A second SIGTERM changed nothing — uvicorn's
handler treats it as the same request again, and only a repeated SIGINT escalates to a
forced exit — so SIGKILL was the only exit left. Force took 12 s on a stop two
normal attempts had spent 444 s failing to complete.

### Why the outage outlived its trigger

The stop's failure was survivable; the force stop worked. What turned it into an
outage is that the rest of the operation belonged to a turn, and that turn was
cancelled *correctly*: it had been inside one tool call for eight minutes, and ending
that is what the watchdog's stall sweep is for. Two independently reasonable decisions composed
into a permanent stop — the stop's deadline (fail the stop, keep the hold, report) and
the watchdog's (cancel a turn that is not progressing). Neither knew what the other
held.

The state that remained made the composition permanent. A started stop is never
auto-released: an explicit hold kept the stopped home from being restarted under an
operator who was still working, which is the right default when the operator is a
person at a console. Its release path was that operator, so a stop whose operator
ceased to exist had no exit. That was true of the mechanism at the time — the hold was
deleted on 2026-10-20, and what survives this failure is the unrescued operation, not
the hold. Nothing anywhere recorded that this home was *supposed* to be serving again
by a declared time, which is why a maintenance window and an outage were
indistinguishable from outside.

### Why nothing caught it earlier

- *Tests.* The launch parameters were assembled inside `main()`, where no test could
  reach them, and no test started a real server with a live stream and a SIGTERM. The
  stop path's behaviour under load was reviewed as code, not exercised as a process.
- *The stop's deadline* did its job: it reported the failure, kept the hold and
  exited non-zero. A deadline with no escalation path hands the last step to a human,
  and it assumed the human was there.
- *The delivery watchdog* judged one turn, correctly. It had no notion of what that
  turn owed the world: a cancelled turn is a local event, and this one's blast radius
  was the whole fleet.
- *The drill's design* put the control room and the operation's record inside the
  fleet being stopped, so the leg's own observers shared fate with its target.
- *Monitoring* is treated separately below: it worked, and it was not enough.

## Detection: what worked, and how far it reached

In-band, everything died with the plane. Monitor wakes retried a bounded budget and
gave up — 153 failed deliveries on one downstream line alone between 09:04:09 and
10:34:14, one-shot results lost, scheduled work silent. That is the expected shape:
every agent-side observer, and every escalation that routes through an agent, was
inside the thing being stopped.

Out of band, one probe on a second machine watched the same surface independently. It
recorded the first refusal at 08:42:52, fired after 302 s and six consecutive
refusals at 08:47:54 (the platform accepted the alert), logged 122 refusals without a
gap, and announced recovery at 10:45:10 and 10:46:11. It never restarted and no hold
touched it.

So this was not a detection failure. Detection was correct and roughly two hours
earlier than the recovery. What was missing was the responder the alert could reach: a
named actor outside the blast radius, holding the staged commands and pre-authorized to
use the force tier. An alert that ends at a notice channel is a message, not a
response. That distinction is why the rule below asks for an actor and not merely for
a signal.

## Guardrails added

- **A bounded drain** (#2712, same day). `serve_kwargs` hands uvicorn
  `gateway.gateway_graceful_shutdown_timeout_seconds` (default 30 s); past that budget
  uvicorn cancels the remaining request and stream tasks and runs lifespan shutdown, so
  a stuck stream costs the budget and never the stop. meiva-root's stop ceiling for the
  gateway is derived from the same setting plus the lifespan allowance, so a gateway
  still draining is never reported as a failed stop.
  `gateway/tests/bootstrap/test_server_shutdown.py` launches the production server
  class in a child process, holds a live stream open, sends SIGTERM and asserts a
  bounded exit, the generator's unwind and lifespan shutdown; the parent owns an
  independent kill deadline so the regression fails instead of hanging. Dropping the
  setting fails the stuck-stream test — the guard was verified by reversing it.
- **Streams end themselves.** `gateway/http/middleware/stopping.py` marks the moment
  the server begins shutting down, and a long-lived stream polls it between frames and
  ends, so the ordinary SSE client reconnects at once instead of spending the budget.
  The budget remains the bound for whatever does not end.
- **No remote dispatch holds the cleanup.** The TTL reaper is its own service, and its
  stop cancels in-flight dispatches rather than waiting for the rest of a serial batch
  of unreachable-machine calls.
- **The hold was deleted** (2026-10-20). There is no `run/deploy-pause-owner.json` and
  no non-expiring state after a stop; the decision lists "a stop left a hold nobody
  released" among the failures that no longer exist, because a stop now leaves only
  `run/lifecycle-op.json`, which gates nothing
  ([delete the maintenance hold](../decisions/runtime/processes/2026-10-20-delete-maintenance-hold.md)).
  That removed *this* failure; it did not guard the class — a turn that dies mid
  operation still dies mid operation.
- **A stop now settles turns, in-band.** The agent host writes one restart per running
  turn on SIGTERM and drains for 30 s; a restart interrupts after a grace and the apply
  queues the note that resumes the turn; a force-terminated wedged turn commits its
  recovery wake in the same transaction. Each of these is a real guard for a live home,
  and none of them can rescue a stop-class window: the agent that would resume has no
  host left to run on.
- **The stop-class drill rule** (#2710, rewritten 2026-10-20). Keep the operation's
  progress record and its recovery access off the stopped plane; record the captured
  operation, its process identities, the current phase, the intended resource scope and
  the actor responsible for recovery *before* the stop; a surviving actor may inspect
  native evidence and continue the recorded operation, and must verify the serving gate
  before declaring it complete
  ([graceful maintenance](../conventions/operations/graceful-maintenance.md)). The
  original amendment also wrote down a 10-minute silence bound judged on observable
  evidence, a staged rescue ladder per phase with a pre-authorized force tier, a bounded
  hold with a named rescuer, and running the operation as a task that a successor can
  carry across turns.

Unguarded, relying on the rule alone: nothing in the system knows a home was supposed to
be serving by a declared time, so no detector can tell a window that ran long from a
maintenance stop, and "the window is over" still ends at a person or an agent outside
the home. A stop-class window run the same way — an agent turn issuing the stop, the
plan living in that turn — reproduces this class with a different trigger; only the
named outside actor changes the outcome. Two production stops after the fix exited cleanly in 0.41 s and 0.21 s
(2026-10-16, 2026-10-17), so the trigger is measured, not assumed; the response gap is
the part that has no guard.

## Unclosed

- **The out-of-band recovery path has never been exercised end to end.** The probe
  proved it can detect and announce; no record shows an off-plane actor receiving that
  signal, running the staged ladder and verifying the serving gate on a deliberately
  stopped home. That confirmation is waiting on an isolated preview venue (#3812) and a
  user-authorized window, and it is not closed here.
- A stop-class operation that is not a drill — a data-plane move, a wave — uses the
  same rule and has the same manual ending.

## Lessons

- An operation that removes the message plane must be rescued from outside it. Its
  executor, its log, its monitors and its escalation paths are all inside the blast
  radius by construction; the only three things that can survive are a named actor on
  another machine, a record on another machine, and a staged command the actor is
  already authorized to run.
- "The stop exited 0" is one leg of a window, not the window. A window has an intent —
  down until something, then up — and if that intent is written nowhere, its end is a
  human obligation that nobody has been told they hold.
- A correctly cancelled turn can still be an outage. A watchdog judges one turn, but a
  turn can own an operation; pair the cancellation with what the turn still owed, or
  the platform's most reliable self-protection becomes its most reliable way to strand
  work.
- A hold that cannot expire must name its rescuer. A state whose only exit is a human
  is a manual-recovery dependency, and the human may be a model turn with a budget.
- Detection without a responder is a message, not a response. Count the actors, not the
  alerts.

The drain half of this incident is the general rule in
[`conventions/defensive-patterns.md`](../conventions/defensive-patterns.md#a-shutdown-drain-is-a-deadline-not-a-wait)
("A shutdown drain is a deadline, not a wait"); this record is its first evidence. The
rescue rule is
[there too](../conventions/defensive-patterns.md#a-stop-that-removes-the-message-plane-needs-an-outside-rescuer).
