---
type: doc
title: Pause markers a start removes
description: Why a leftover run/deploy-pause-owner.json is worse than litter, what a start checks before removing one, and which other run files it leaves alone.
tags: [cli, lifecycle, operations]
---

# Pause markers a start removes

`meiva stop` used to take a maintenance hold: `run/deploy-pause-owner.json` plus the lock beside
it, walked through phases and left on disk. The hold was deleted on 2026-10-20
([decision](../../../../docs/decisions/runtime/processes/2026-10-20-delete-maintenance-hold.md))
and nothing reads or writes the file any more.

Ignored is not gone. After the 2026-10-21 wave every host was serving with a marker on disk
saying `paused`, written by the previous generation during its own stop. Anything that gates on
that file then reads a stop that never ends: this repository's wedge watch skipped its own checks
for half an hour before the marker was found. A wrong record that no code maintains is worse than
litter.

## What decides

The live fact, never the file's age and never the command that wrote it. A start sweeps the
markers at the end of its readiness phase — the moment the home's claim to be `paused` becomes
false — and removes one only where it can see the contradiction: the agent host's pid is running
and meiva-root holds its instance lock. A home that is not serving keeps every marker and says
so, because nothing there proves the claim wrong. Each removal and each decision not to remove
prints one line in the start's output.

The same rule reads the stop journal: `run/lifecycle-op.json` records that a stop completed, but
only a liveness check turns that record into "this home is stopped now"
([[cli/commands/lifecycle/docs/readiness-verdict.meiva.okf.md]] owns the readiness that gates
this phase).

## What it deliberately leaves alone

`run/` holds three kinds of file, and only the third is a claim a start can disprove:

| Kind | Examples | Why untouched |
|---|---|---|
| Current writers and readers | `lifecycle-op.json`, `start-serving.json`, `bootstrap-snapshot.json`, `agent_host.pid`, `pty-sessions.json`, `*.sock`, `meiva-root/` | Live state, rewritten on every start |
| Written outside this repository | `probe-metrics-pub-state.json`, `staging-probe-v0-state.json` (Monsora staging probes), `db-write-stall-watch.json` and `review100-keeper.json` (stored schedule scripts) | Their owner maintains them; a file this checkout cannot write is not evidence of a stale release |
| Orphaned records, no writer and no gate | `vp-boundary-crossed`, `.gauge_state`, `patrol-state.json`, `update-baseline.json`, `network-notes-*.json` | Diagnostic records: harmless to a reader, and deleting another tool's history is not the start's business |

A general "remove what no code writes" rule would take the third row with the first — the sweep
names one family whose mechanism was deleted, and grows only when another mechanism is deleted
the same way.

## How it is proven

`base/deploy/lifecycle/tests/test_pause_markers.py` holds the live fact itself: a marker on a
serving home goes and the line names it; a home whose agent host has exited, or whose meiva-root
does not hold the lock, keeps every marker; a `run/` with no marker produces no line at all.

Parent: [[base/deploy/lifecycle/docs/home_lifecycle_locks.meiva.okf.md]].
