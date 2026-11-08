---
type: doc
title: Events Maintenance — unnamed-writer audit count
description: "The audit-identity pass that makes `audit_events.machine = 'unknown'` observable — what it counts, why the window and the day total answer different questions, and what silence means."
tags: []
---

# Events Maintenance — unnamed-writer audit count

## What it is
`services/upkeep/events_maintenance/audit_identity.py`, a step of the daemon's existing maintenance
pass (it adds no loop and no schedule). Task #6235 is the read side of `#6089`: that change made
`audit_events.machine` either a canonical machine name or the literal `unknown`, replacing a fallback
to a hostname. The column stopped being plausible when a writer loses its identity — and nothing read
it, so the degradation stayed silent.

## What it counts
One statement against `audit_events_canonical` (the single read path for the machine dimension;
rows whose machine cannot be named keep `unknown` there):

- **the trailing window** — `window_seconds` is one `MEIVA_EVENTS_MAINTENANCE_INTERVAL_SECONDS` plus
  the pass deadline, so a slow pass cannot open a gap between two counts. This is the reading the
  event fires on: a cluster that attributes every row it writes reads zero.
- **the trailing 24-hour total** — what a day book asks, how many rows today do not say who wrote
  them. It rides along in the payload as context and is deliberately **not** a trigger: on its own it
  stays non-zero for a day after a single row, and a number that is usually non-zero teaches nothing.

The window splits by whether the row carries an agent. Both halves mean the writer could not resolve
its machine; `agent_rows` additionally means a running agent process did.

## What it emits
One `audit_machine_unknown` event (`base/events/declarations/observability.py`, tier `anomaly`) per
pass that saw at least one such row, carrying `user_rows`, `agent_rows`, `window_rows`, `day_rows` and
`window_seconds`. A pass that saw none emits nothing. The name is listed in the
`core_events_what_happened` board query, so a row reaches the events view through the log stream; no
Postgres reader queries it by name, so it is not persisted.

## What silence means
No event means every audit row written during that window named the machine that wrote it — the
invariant `#6089` established was satisfied. It does not mean the counters were verified end to end:
an audit writer that stops writing produces no unnamed rows either. `audit_write_failed` covers the
writer's own failure; this pass covers its attribution.
