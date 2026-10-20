---
type: doc
title: Turn interrupts (cancel and terminate)
description: Which intent stops a running turn, what co-batched chats and newer inbounds veto, and what each node keeps of an interrupted step.
tags: []
---

# Interrupting a turn: cancel and terminate

Public cancel (`POST /api/agents/{id}/cancel`) is not an inbound: it asks the
home host to cancel the running turn Task, so the in-flight node is cancelled
where it awaits and the turn ends without a claim pass. The agent stays alive
and idle and its next input starts a fresh turn. See
[[agent/graph/exec/docs/hosted-turn-cancel.meiva.okf.md]].

Terminate remains a durable inbound. The two-second DB poll in
`agent/graph/interrupt.py` observes a pending cancel/terminate row during
LLM/exec, halts and returns to claim; claim routes termination to `END` while a
`kind='cancel'` inbound keeps the agent alive and idle. No public route writes a
`cancel` row today; the consumer remains for external controllers.

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
  `[cancelled by system]` for maintenance or another external system command.
  The watcher retains that attribution through subprocess cleanup; a later
  inbound cannot relabel it. Existing side effects are not undone or replayed.

An accepted native cancel remains bound to its original work across action
boundaries; a later chat or work UUID cannot consume that command.
