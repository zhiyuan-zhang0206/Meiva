---
type: doc
title: Historical agent attribution by view, not by rewrite
description: Why the turn_end/exec rows that lost agent_id are attributed at read time, and what the view guarantees.
tags: [telemetry, database, audit]
---

# Historical agent attribution by view, not by rewrite

## The gap

The emitter stopped passing `agent_id` at the emit site on **2026-10-09 22:22Z**.
`turn_end` was fixed on 2026-10-16 (`aa4eaa9c5`); the `exec` / `code` family is
still open (task #6921). The rows carry no owner of their own:

| event | NULL `agent_id` rows | share with a trace |
|---|---:|---:|
| `exec` | 148,409 | 128,301 |
| `code` | 149,523 | 129,287 |
| `turn_end` | 111,483 | 91,653 |
| `plugin_activation` / `halt` / `syntax_fix` / `passive_recall` / `recall_filter` | 40,630 | ~34,500 |

## Why not rewrite the rows

`telemetry_events` is append-only by decision
(`docs/decisions/data/database/2026-10-02-telemetry-events-in-postgres.md`):
UPDATE, DELETE and TRUNCATE are rejected by trigger for every role, superuser
included, and the application logins hold SELECT and INSERT only. A backfill is
therefore not a permission problem to be solved with a wider grant — it would
mean disabling a trigger to rewrite the record of what happened, and the
rewritten rows would afterwards be indistinguishable from rows that always had
an owner.

## What we do instead

Two objects, both created by
`migrations/2026/10/18/20261018T142500_telemetry-owner-inference.sql`:

- **`telemetry_owner_inference`** — one row per trace whose `llm_usage` rows
  name exactly one agent (`evidence = 'llm_usage_trace'`). Seeded once from the
  live stream; it is derived data, append-mostly, and a fresh cluster simply
  starts empty.
- **`telemetry_events_attributed`** — every `telemetry_events` column, with
  `agent_id = coalesce(row.agent_id, inferred.agent_id)` and an extra
  `agent_id_source`:

  | value | meaning |
  |---|---|
  | `observed` | the row carried this `agent_id`; nothing was inferred |
  | `inferred` | the row had none; the value comes from its trace's `llm_usage` |
  | `unattributed` | the row had none and its trace names no single agent |

An inferred value never looks like an observed one: readers that must not accept
inference filter `agent_id_source <> 'inferred'`.

## Coverage and window

Measured 2026-10-18 22:22Z against the live stream (2.87M rows):

| event | NULL rows | inferable | coverage |
|---|---:|---:|---:|
| `exec` | 148,409 | 128,265 | 86.4% |
| `code` | 149,523 | 129,252 | 86.4% |
| `turn_end` | 111,483 | 91,622 | 82.2% |
| `plugin_activation` | 16,891 | 14,448 | 85.5% |
| `halt` | 12,473 | 10,627 | 85.2% |
| `syntax_fix` | 3,220 | 2,771 | 86.1% |
| `recall_filter` | 3,051 | 2,953 | 96.8% |
| `passive_recall` | 4,995 | 3,914 | 78.4% |

Coverage is `inferable / NULL rows of that event`. It is deliberately not a
share of all ownerless rows: of the 1,520,855 rows whose `agent_id` is NULL
live-wide, 1,125,096 (74%) carry no `trace_id` at all and can never be joined —
most of them belong to events that never carry an owner (`gateway_latency`,
`log`, `llm_usage`, the delta families). Within the emitter-gap events above,
78–97% of their NULL rows do carry a trace.

There are **0 traces with more than one owning agent**, so the map is
unambiguous by construction. `turn_end` stops at 2026-10-16 13:08Z (the fix
landed); `exec` / `code` keep growing until task #6921 ships — those new rows
enter the view as `unattributed` and become `observed` once the emitter passes
its own `agent_id`.

## Who creates the objects

Both objects must be created by the schema owner (`meiva_main`). PostgreSQL's
default privileges only cover objects a role creates *after* they are set, and
the birth-time `GRANT ... ON ALL TABLES` has long finished on a live cluster; a
table created later by another account therefore has no role ACL at all. The
2026-10-18 live apply was exactly that case: the objects existed, the migration
stamp recorded, and every service role got `permission denied for view`. The
repair handed ownership to `meiva_main` and granted `SELECT` to `meiva_runner`
and `meiva_gateway`. The migration now states those grants itself, and the
migration smoke `SET ROLE`s into a service role to prove a non-owner can read.

## Seeding

The migration creates the objects only. The seed is one data step, run by the
database owner and re-runnable:

```sql
INSERT INTO telemetry_owner_inference (trace_id, agent_id, evidence)
SELECT trace_id, min(agent_id), 'llm_usage_trace'
FROM telemetry_events
WHERE event_name = 'llm_usage' AND agent_id IS NOT NULL AND trace_id IS NOT NULL
GROUP BY trace_id
HAVING count(DISTINCT agent_id) = 1
ON CONFLICT (trace_id) DO NOTHING;
```

Nothing schedules this seed today: new traces arrive without a row here, so the
inferred share decays until it is re-run. Re-run it when the emitter gap (#6921)
closes and on any review of the unattributed volume.

Parent: [[docs/decisions/data/database/2026-10-02-telemetry-events-in-postgres.md]].
