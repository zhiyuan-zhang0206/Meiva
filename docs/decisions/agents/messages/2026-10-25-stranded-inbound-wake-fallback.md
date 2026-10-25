# A stranded inbound gets a bounded wake fallback

## Context

Delivering a chat is one of two orders, and only one of them had a wake path.

The primary path is event-driven: `ops.lifecycle.deliver_chat_inbound` calls
`resurrect_if_terminated` at write time, so a message written to an owner that
already reads `terminated` wakes it immediately. The reverse order has no
consumer. The message commits while the owner still reads `running`; the owner
finishes dying before its claim reaches the row; the row stays `pending`. The
agent host polls `idling`/`running` owners only
(`services/agent_runner/agent_host/scheduling/pending_wakes.py::scan_rows`), and
nothing else scans a terminated owner. The row then waits for the next message
to the same owner, or for the dead-letter sweep to retire it 24 hours later
(`dead_letter_stale_pending_chats`).

2026-10-25 (agent 6984, inbound 345099): a peer's second-round instruction landed
0.6s before the owner's death was recorded. The delivery path read a live owner,
the terminate command was already claimed, and no path picked the row up for 67
minutes. It cleared only because the sender happened to send a follow-up, which
re-ran the delivery path. The same shape can stall a long task with no one
noticing: the sender believes the work is queued, the owner is gone.

The terminate veto in the claim node
(`agent/graph/claim/_routing.py::_terminate_veto`) is the other guard against
this, and it is deliberately not a fence: a durable lifecycle command that was
already accepted cannot be vetoed by a later message. The fallback therefore
cannot live in the claim path.

## Decision

`services/wake/delivery_watchdog/stranded_wake.py` adds one resident loop beside
the stall scan. While a terminated owner holds pending work that passed the
resurrection fence, the loop runs the wake the delivery path would have run
(`ops.lifecycle.resurrect_if_terminated`), so placement, the pause latch, the
recovery breaker, the notice refusal and the home-side CAS all stay in the one
existing implementation.

- **Fallback, never primary.** A message written to an already-terminated owner
  is woken by delivery; the loop only sees what delivery could not have woken.
- **Fence.** `TRIGGER_AFTER_KILL_COMMAND` (now shared by the CAS and the scan):
  the trigger must be newer than every terminate command of its owner, and must
  either postdate the death or belong to a death a command ended
  (`termination_source = 'user'`). Work queued before a kill cannot reverse it, and
  an involuntary death (crash, failed launch — no command mark) still never wakes
  for leftover work.
- **Throttle.** One attempt per owner per
  `delivery_watchdog_stranded_wake_cooldown_seconds` (default 300s), claimed by a
  single `UPDATE` that stamps `agents_meta.wake_suppressed_until` with reason
  `stranded_wake` — durable across restarts, per owner rather than per message.
  At most `delivery_watchdog_stranded_wake_max_per_round` owners (default 2) per
  round. Past `delivery_watchdog_stale_claimed_threshold_seconds` the dead-letter
  sweep retires the row and the fallback stops selecting it.
- **Gates.** The recovery breaker and an active suppression window exclude an
  owner entirely: an until-human halt is not resumed by a timer, and the
  until-human window is never overwritten by the cooldown.

## Why the fence changed

The CAS required `m.created_at > agents_meta.status_changed_at` — the trigger had
to postdate the death. The reported instance did not: it arrived while the owner
was still shutting down. The fence is now two conditions:

1. the trigger is newer than **every** kill command of its owner (the id fence,
   unchanged); and
2. either it postdates the death, **or** the death was a commanded one
   (`agents_meta.termination_source = 'user'`).

The second half is a death mark, not a history lookup. A first draft asked
whether the agent had *ever* received a terminate command; review showed why that
is wrong: after a resurrection, the previous life's command still satisfies such
an `EXISTS`, so an involuntary death in the next life would have been woken for
leftover work — the exact invariant this design claims. `termination_source` is
stamped per death and cleared on resurrection, so it answers "did a command end
**this** life".

That matters because every commanded death on this runtime is stamped `user`
today, including an agent's own `terminate` and a peer's: the write sites are the
force paths (`ops/lifecycle/termination.py`, `agent/ownership/hosted.py`), and the
old `exit` stamp has no writer left. So "commanded" and "the user personally
killed it" are not distinguishable in this column — which is why the rule is
stated as a property of the death, not of the killer.

## The constraint this narrows

`lifecycle_acceptance` (before 7e3bdd9f6d) recorded: work that predates a system
reap is still work to resume, *unlike work predating a user's explicit death*
(task #3617, design #3610 section 6). That constraint is preserved in substance
and narrowed in time: work queued **before the kill command** still cannot resume
an owner through any wake path (the id fence, unchanged, and the reason a repeated
force keeps its fence). What is newly admitted is work queued **after** the kill
command and before the death was recorded — work that the sender addressed to a
live owner and that the dying incarnation never claimed. The old rule treated the
death instant as the boundary; the boundary is now the decision that preceded it.

## Alternatives rejected

## Alternatives rejected

- **Relax the CAS to "any pending work"** (drop the death fence entirely). A
  crash corpse would then be revived for arbitrarily old work, reversing the
  2026-10-11 simplification without a decision.
- **A watchdog scan that resurrects without a trigger.** It would need its own
  copy of the breaker, notice and placement gates, and the home-side CAS would
  have no row to verify — exactly the stale-in-flight-attempt race the trigger
  CAS exists for.
- **Defer the termination when work is pending.** The terminate command was
  accepted and serialized against the owner; making an arriving message able to
  cancel it turns the agent's own stop into a suggestion.
- **Drain the rows at death.** Consuming a message the owner never read is a
  silent loss, which is the defect being fixed.

## Consequences

- A message stranded by a commanded death (an agent's own terminate, a peer's, or
  an operator kill) is woken within one scan interval instead of waiting for the
  next message.
- An involuntary death — crash, failed launch — still strands leftover work for
  24h; only work that arrives after the death wakes it. That is the "no replay"
  boundary, and the death mark is what proves the difference.
- `agents_meta.wake_suppressed_until` gains a second writer and a second reason
  (`stranded_wake`), which is what the column was originally documented for
  (`db/schema.sql`); a `permanent_provider_reject` window is never replaced by it.
- Attempt rate has two bounds and one honest worst case. A refused or failed wake
  keeps the 300s cooldown (about 288 attempts/day). A wake that **succeeds** and is
  followed by a death before the row is claimed clears the cooldown, so the same
  owner can be woken once per scan interval — up to about 2880/day. That worst
  case is bounded globally by the per-round cap (2 owners per 30s round, so 240
  wakes/hour cluster-wide regardless of how many owners qualify) and by the
  dead-letter age (the row stops qualifying after 24h). Both bounds are deliberate:
  a hard per-agent cap cannot tell a transient outage from a poison message.
- The fallback emits `delivery_stranded_wake` (anomaly, persisted), with the
  `meiva-ops-delivery-stranded-wake` alert and the "Stranded wake (window)" panel
  beside `delivery_stalled`: the stall alert reports the wait, this one reports
  that the system had to revive a corpse for it.
- An automatic wake that revives an owner a user just killed is now visible to a
  human rather than only to the log.
