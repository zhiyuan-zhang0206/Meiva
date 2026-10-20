---
type: doc
title: Lifecycle Settlement
description: Original lifecycle settlement, admission fences and explicit new wakes.
tags: []
---

# Lifecycle Settlement

## Completion

A restart or terminate command is claimed at a turn boundary and executed by
`apply_hosted_lifecycle` in one transaction, fenced by the incarnation's
generation and owner: the row changes and the command is marked done together,
or neither happens. Claim returns END; the host flushes the checkpoint before
applying. Cache eviction, a NULL PID, an expired lease or a health endpoint
alone cannot prove completion. A command claimed by a host that died before
applying is claimed again by the replacement; nothing needs observing.

Resurrection requires the terminated hosted incarnation's retained generation
and owner, no per-agent PID, and no unresolved exec allocations. The resource check precedes identity release so
receipt recovery can still address the original incarnation. Admission then
uses the released metadata row and closed resource set, without consulting
historical lifecycle receipts or requiring the shared host to exit. A
row with no runtime identity resurrects as a fresh hosted birth instead, in
two cases, each rechecked in the final CAS. One is a never-admitted row whose
birth marker is still unconsumed. The other is a row this runtime
force-terminated while no incarnation owned it: the force records
`unowned_termination` on its own command, but only when the row's unowned
state has a lifecycle origin. That origin is the spawn's birth epoch
(`last_resurrect_inbound_id = 0`), or a `lifecycle_release` receipt on the
resurrection or executed restart that left the row unowned. The receipt must
belong to the current life, that is, have an id above the latest resurrect inbound
(`docs/decisions/agents/lifecycle/2026-09-29-unowned-termination-resurrects.md`).
Historical process runtimes, unknown runtime kinds, incomplete hosted
identities and resources the current model cannot decode refuse resurrection
(`runtime_cutover_required`); no code reconciles them. A terminated row with
NULL resources passes this gate only with a minted hosted identity (a hosted
kind and a fresh generation and owner), which the resurrection CAS clears
(`docs/decisions/agents/lifecycle/2026-09-28-legacy-terminated-agents-resurrectable-at-cutover.md`).
Every other such row keeps refusing, and so does a legacy unowned row this
runtime force-terminated without having released it first.
An automatic resurrection that meets such a refusal leaves its inbound queued
and logs a WARNING naming the reason.
No process-exit observer adopts those rows or settles a hosted logical lifecycle.

Hosted protocol-zero rows with NULL resource metadata retain their existing
settlement behavior. NULL is not a resource-closure proof; the broader resource
and database cutover remains required.

## Force and later commands

Explicit force writes `terminated` in the API's own transaction, closes every
older open command as superseded and asks the home host to cancel the running
turn. Cancelling is acceleration: the terminated row already stops admission
and the turn ends at its next ownership check. Later commands and chat are not
acknowledged as a side effect of force.

## Resurrection supersession

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

A queued wake keeps its original inbound identity. A changed home, paused
machine or unresolved prior execution cannot be bypassed by retrying the wake.

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
