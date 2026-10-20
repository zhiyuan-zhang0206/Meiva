---
type: doc
title: Turn interrupts (cancel, terminate and restart)
description: Which intent stops a running turn, what co-batched chats and newer inbounds veto, what each node keeps of an interrupted step, and how a restart that ends an unfinished turn resumes it.
tags: []
---

# Interrupting a turn: cancel, terminate and restart

Public cancel (`POST /api/agents/{id}/cancel`) is not an inbound: it asks the
home host to cancel the running turn Task, so the in-flight node is cancelled
where it awaits and the turn ends without a claim pass. The agent stays alive
and idle and its next input starts a fresh turn. See
[[agent/graph/exec/docs/hosted-turn-cancel.meiva.okf.md]].

Terminate remains a durable inbound. The two-second DB poll in
`agent/graph/interrupt.py` observes a pending cancel/terminate row during
LLM/exec, halts and returns to claim; claim routes termination to `END` while a
`kind='cancel'` inbound keeps the agent alive and idle. No public route writes a
`cancel` row today; the consumer remains for external controllers. A pending
restart joins the poll after a grace, below.

A terminate **yields to a message the death decision did not see**: a chat
co-batched with a self-initiated terminate (`meiva.self.terminate()` — the chat
arrived during the very turn the agent terminated in), a chat newer than an
external terminate (the latest intent wins, same recency rule as the
resurrect veto), or any pending inbound newer than the whole claimed batch (a
final queue recheck before the exit is committed). The vetoed terminate is a
consumed no-op: no marker, no `END`, never retried — the agent stays alive and
processes the message, so the last message is never swallowed by a racing
death.

Node-level discard semantics differ, and deliberately so:

- **llm_node** discards the partial generation whole — nothing was committed, so
  there is nothing worth keeping.
- **exec_node** keeps partial stdout/stderr and attributes the tool result to
  the first observed interrupt: `[cancelled by user]` for a user command,
  `[cancelled by system]` for maintenance or another external system command,
  `[interrupted by restart]` for a restart. The watcher retains that
  attribution through subprocess cleanup; a later inbound cannot relabel it.
  Existing side effects are not undone or replayed.

## A restart interrupts after a grace and resumes the turn

A pending restart (never the agent's own, `source='self'`, which already ends its
exec by raising) is an interrupt too, but only once its row has waited
`RESTART_INTERRUPT_GRACE_S` (10 s, `base/native_process/stop_timing.py`) on the
database clock; `pending_interrupt_reason` reads the row's `created_at`. Most code
runs end inside the grace and the restart then takes effect at the next turn
boundary with nothing cut short. Cancel and terminate need no grace. The watcher's
poll (`INTERRUPT_POLL_S`) bounds how soon past the grace the node notices, and the
graceful exit's drain is held above grace, poll and one unwind by the clock lattice.

An interrupted node returns `interrupt_halt(reason)`: `halted`, and for a restart
also `interrupted_by_restart`. That mark exists because `halted` cannot say whether
the turn is over: an agent that finished or was paused is halted, and so is a turn a
restart cut short. The claim that takes the restart (`handle_restart`) calls the
agent idle only if it is halted without the mark, or has no history, and the
restart is not the agent's own; `meiva.self.restart()` ends its exec halted with the
turn unfinished, so it never counts as idle. After the claim `halted` is that
verdict: set, the agent was idle and reads "Restart was accepted from ..."; clear,
the turn was unfinished and claim adds no marker. The claim clears the mark, and
every invocation's input does.

The host reads the verdict when it applies the restart
(`finish_completed_invocation`: `restart_requested` and not `halted`).
`apply_hosted_lifecycle` then queues, in the transaction that executes the restart,
one pending `system_note` (`lifecycle_restart`, the restart's own source) from
`restart_resume_note`: who asked, that running code was terminated with its result in
the matching tool message, that shell sessions may not have survived. Nothing else is
asserted, because the writer does not know it. Once the apply has committed, the host
wakes the agent through its own scheduler (`AgentHost.bind_wake`), so the next turn
starts when this one ends; a host that is exiting refuses the wake, and the pending scan
finds the note on the next host (it also covers a host that bound no scheduler). The
claim renders the note and the model runs from the checkpoint. The held-control path (maintenance hold, impersonation lease) runs no
graph and never queues the note; a hard kill or a cancelled drain leaves the
checkpoint's turn abandoned, since each turn's first invocation starts halted.

An accepted native cancel remains bound to its original work across action
boundaries; a later chat or work UUID cannot consume that command.
