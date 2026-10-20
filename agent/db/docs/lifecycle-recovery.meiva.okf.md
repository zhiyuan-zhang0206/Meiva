---
type: doc
title: Lifecycle Settlement
description: Lifecycle settlement and explicit new wakes.
tags: []
---

# Lifecycle Settlement

## Completion

A restart or terminate command is claimed at a turn boundary and executed by
`apply_hosted_lifecycle` in one transaction, fenced by the incarnation's
generation and owner: the row changes and the command is marked done together,
or neither happens. Claim returns END; the host flushes the checkpoint before
applying. Cache eviction or a health endpoint alone cannot prove completion. A command claimed by a host that died before
applying is claimed again by the replacement; nothing needs observing.

Resurrection moves a terminated row to idling and clears its runtime identity; the
next admission mints a new generation. Any terminated row can be resurrected, and an
automatic resurrection needs a pending trigger newer than the termination and newer
than every terminate command of the agent.

A resurrection inserts its `kind='resurrect'` inbound and closes every older
open restart/terminate command as superseded (the payload names the resurrect
inbound), so a command queued in the previous life cannot act on the next.
A command created after the resurrect inbound is current intent and can still
stop the new incarnation. A command that commits after the resurrection
transaction but carries an older id is not recognized; the id order is not
re-checked at claim.

## Resurrection and user wakes

`ops/agents/wake.py` locks home placement and the pause latch, verifies the
terminated state, inserts the resurrection marker, supersedes older commands
and optional work, then publishes the wake. A refusal rolls the whole
transaction back. The agent host admits the successor; no OS launcher is
involved.

A queued wake keeps its original inbound identity. A changed home or paused
machine cannot be bypassed by retrying the wake.

A corpse reap records termination and diagnostics. It produces no recovery chat,
resurrection request or retry job. The delivery watchdog does not resurrect
terminated owners.

An ordinary fresh chat may wake a terminated agent through the normal delivery
path. A permanent provider rejection blocks background wakes immediately;
explicit new chat or per-agent operator resurrection may retry after a fix.
A cancelled or interrupted turn is not reconstructed from an old checkpoint;
the next invocation starts halted and waits for fresh input.

## Entry Points

- `agent/ownership/hosted.py` — native completion and resource settlement
- `ops/agents/wake.py` — transactional resurrection
