# A restart interrupts after a grace, and the turn it ended resumes from a queued note

## Context

The graceful exit ([the earlier decision](2026-10-20-host-graceful-exit-writes-own-restart.md))
gave every running turn a restart and let the next host's scan wake the agent through
the lifecycle pointer the applied restart left on `agents_meta`. Removing the host's
self-fencing (one host per machine and home, no lease, no per-command pointer) removed
that pointer, so an applied restart left nothing that wakes an agent whose turn it had
ended: the checkpoint says the turn is unfinished, but the pending scan sees only a
pending inbound, a claimed restart or terminate, or an impersonation lease. Under the
pointer the continuation had also been weaker than it read: the first invocation of every
turn starts `halted` on purpose (a continuation an interrupted predecessor left is
abandoned), so a wake with no pending input ended at its first claim without asking the
model.

A restart that waits for the turn boundary also waits for the turn: a long
`execute_code` or model step holds the restart, and the graceful exit's drain with it.

## Decision

1. **A restart interrupts after a grace.** The interrupt watcher already polls
   `pending_interrupt_reason` for a pending cancel or terminate. A restart (not the
   agent's own, `source='self'`) now counts too, once its row has waited
   `RESTART_INTERRUPT_GRACE_S` (10 s) on the database clock. Most code runs end inside
   the grace and the restart lands at the turn's next boundary; a call or run still in
   flight is interrupted. Cancel and terminate interrupt at once. The grace is the
   restart row's own `created_at`, so the constant has no bypass and tests age the row.
2. **An interrupted node says why.** `InterruptReason.RESTART` ends a model call (the
   half generation is dropped, as for any interrupt) or a code run (the child is killed,
   its output kept, the result marked `[interrupted by restart]`). Both set `halted`, as
   for a cancel, and also `interrupted_by_restart`.
3. **The claim that takes the restart judges the agent.** `halted` alone cannot separate
   an idle agent from one whose turn a restart ended, because the interrupted node sets
   it. The claim treats the agent as idle only if it is halted without that mark, or has
   no history, and the restart is not the agent's own. An idle agent reads "Restart was
   accepted from ..." as before. Any other agent had an unfinished turn: claim adds no
   marker, and the turn's `halted` stays clear. The host passes that verdict to the apply.
4. **The apply queues the note that resumes the turn.** In the transaction that executes
   a restart for an unfinished turn, `apply_hosted_lifecycle` inserts a pending
   `system_note` (`note_tag='lifecycle_restart'`, the restart's own source) saying who
   asked, that running code was terminated and where its result is, and that shell
   sessions may not have survived. The pending scan wakes the agent on whichever host
   runs next; the claim renders the note and the turn runs the model from its
   checkpoint. The restart is never done without the note. External restarts (user,
   gateway, hold, graceful exit) and the agent's own `meiva.self.restart()` resume
   alike. The held-control path runs no graph and queues none.
5. **The graceful exit loses its pointer dependence.** It still stops admission, writes a
   restart per running turn (reusing an unfinished lifecycle command) and waits within
   `HOST_GRACEFUL_EXIT_DRAIN_S`, which must exceed the grace, the interrupt poll and one
   unwind bound; a lattice constraint says so. A turn that outlives the drain is named at
   ERROR and cancelled.
6. **A hard kill resumes nothing.** SIGKILL and crashes write no restart and no note, as
   decided before; a cancelled turn's successor takes the restart as an idle agent's.

## Consequences

- A restart is no longer held back by a long run: it waits at most the grace plus one
  poll. A hold-driven drain gets the same bound.
- A user restart of a working agent now resumes it, where it used to leave the agent
  idle with a marker.
- The checkpoint gains one boolean channel, cleared by the claim that reads it and by
  every invocation's input.
- The note is consumed when the successor claims it; a crash between that claim and the
  checkpoint flush loses it, like any other non-chat inbound.
