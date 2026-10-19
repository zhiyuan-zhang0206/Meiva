---
type: doc
title: Adversarial weekly evaluation
description: Case rotation, isolation overlays, worker completion and restart recovery of the weekly batch.
tags:
- schedules
- evaluation
---

# Adversarial weekly evaluation

The batch fires Wednesday 04:00 cluster time and rotates five of the nine
scenario cases by ISO-week parity: even weeks run c001, c002, c004, c006 and
c008, odd weeks c001, c003, c005, c007 and c009. Each case builds a disposable
scenario directory under `~/.meiva/adversarial_eval/cases/<case>`, and the
scoreboard and index land in `results/` beside it.

Every probe runs under an `eval_isolation` overlay; c003 additionally allows the
`web` tool through the overlay's network allowlist because its partner document
is served on loopback for the batch.

A worker counts as done only when it can no longer act: `TERMINATED`, or
`IDLING` with output already produced. A freshly spawned agent's row starts as
`IDLING` before its process has even booted, so bare `IDLING` read the whole
batch as finished seconds after spawning and audited empty transcripts
(adversarial-eval 2026-35, 2026-08-26). A worker that finished but failed to
terminate still ends its turn `IDLING` with the report message the audit reads.

The batch marker lists the agents spawned by the current batch and is replaced
atomically, so a restarted process reads either the previous or the new list. A
marker left behind by a restart records the abort, terminates those agents and
wakes the owner.

Accepted limitation: a worker that follows c007's bait spawns a non-isolated
child because its own spawn call carries no overlay. The audit flags that child,
but automated cleanup of such children does not exist yet; this is accepted
until OS-level sandboxing.

Worker prompts and scenario material carry required CJK phrasing, escaped as
`\uXXXX` because raw CJK is only allowed in the frontend locale files.

The scoreboard, alerting policy and the batch's loopback server are described in
`schedules/README.md`.
