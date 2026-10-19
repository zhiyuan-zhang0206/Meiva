---
type: doc
title: Turn interrupts (cancel and terminate)
description: Which intent stops a running turn, what co-batched chats and newer inbounds veto, and what each node keeps of an interrupted step.
tags: []
---

# Interrupting a turn: cancel and terminate

Public cancel uses the dedicated native command tied to one observed graph
work UUID, not the generic inbound queue. The two-second interrupt watcher
checks that exact work's command; claim preserves co-queued chat, and the host
settles the command only after committed halt/checkpoint and resource-closure
proof. See [[base/agents/incarnation/docs/native-work-cancel.meiva.okf.md]].

Terminate remains a durable inbound. The same DB poll in
`agent/graph/interrupt.py` observes it during LLM/exec, halts and returns to
claim; claim routes termination to `END` while cancel keeps the agent alive and
idle. Retained native cancel envelopes follow the existing history/claim
contract, but there is no public `/api/cancel` producer.

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
