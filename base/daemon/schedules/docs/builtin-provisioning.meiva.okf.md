---
type: doc
title: Built-in schedule provisioning
description: How the manifest becomes schedule rows — create with the manifest default, resync a drifted script, and leave an existing row's state alone.
tags:
- base
- schedules
---

# Built-in schedule provisioning

`schedules/manifest.json` is the single expression of the built-in policy: a
`product` schedule ships enabled, an `operator` schedule ships disabled and waits
for the operator to enable it. Provisioning reads that manifest and touches one
row per entry:

- a missing row is created with the manifest's `default_enabled`, plus the same
  `initial` version snapshot the API create writes;
- a row whose `script` or `command` differs from the template is resynced to it,
  writing a `schedule_versions` row (`builtin-resync <hash>`) and queueing a
  `schedule_sync_requests` row so a live session relaunches onto the new code;
- everything else stays: `enabled`, `description`, status and launch counters, so
  `meiva schedules stop <name>` keeps a built-in stopped and a deleted built-in
  returns with the manifest default.

The checkout is the source of truth — a hand edit of the stored script is
replaced at the next provision, so change the template instead. Agent-created
schedules are not in the manifest and are never read or written here. Both call
sites (the gateway lifespan and `meiva schedules provision`) pass their own
connection, and provisioning is idempotent against an unchanged checkout.

The operator-facing table and the deploy procedure live in `schedules/README.md`;
the start-time seeding flag lives in `gateway/schedules/docs/schedules.meiva.okf.md`.
