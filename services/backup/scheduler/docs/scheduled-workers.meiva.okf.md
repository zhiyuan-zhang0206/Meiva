---
type: doc
title: Scheduled Backup Workers
description: Private staging, bounded worker stop and validated atomic publication for scheduled logical backups and restore drills.
tags: []
---

# Scheduled backup workers

`worker_process.py` launches the fixed worker module from this checkout in an
isolated interpreter and new process group. The bootstrap checks code origins.
Secrets travel on stdin, never retained request files. Each run has a unique
0700 `.operation-*` directory, private request/result files and file-backed
stdout/stderr. Progress and large artifact commits stay off the event loop.

Each scheduler process reads its `SchedulerSettings` (cluster clock, backup hour, retention and the
captured throwaway base) once at its root; every field is `restart_required: gateway`. Each operation
worker reads its `DumpSettings` (plane, retention and off-site destination) once at process entry.
Below those roots a function takes the values, never the config owner: the pure calendar lives in
`services.backup.schedule`, the dump's argv, names and retention in `services.backup.dump_plan`, and
the commit judgements in `commit_plan.py`. One `BackupTree` value names every path under
`<home>/backups`.

Schema admission, scheduler health ports and log database dials use that same
scheduler boot. Endpoint tables are rebuilt at the original resource reads
through `ServiceEndpoints.from_boot` with the root's runtime directory. The
separate worker's log/work/scratch database factories (`worker_database`) read its own boot when
a handle is built, because delivered database credentials rotate under a running process; scratch
handles replace only the requested URL. Neither
entry recovers configuration from the legacy settings singleton.

The kind's concurrency lock prevents overlapping controller commits. A busy
lock defers the scheduler. A zero worker exit and object result precede
`CompletedOperation.commit`: dump publication validates the name and digest,
then links the exact encrypted artifact exclusively into the backup directory.
Cross-filesystem publication verifies a private copy before linking it. A
failure never advances the scheduler/restore success marker.

Cancellation gives the known worker/group a bounded TERM grace, then KILL and
a bounded direct-child wait. Normal worker cleanup removes keys, plaintext
partials and decrypted scratch. The controller also sanitizes its private
staging on failure or after commit. Cleanup errors retain an actionable path;
they are not hidden behind a successful operation verdict.

There are no process/family receipts, all-descendant disappearance proof,
quarantine, restart adoption, held unreaped-leader registry, blocked kinds or
`meiva backup operations` retirement commands. Old control directories are not
read as process authority; the next run has independent scratch. Unexpected
orphan processes require operational investigation, not an automatic census.

Restore drills use existing unique throwaway PGDATA and its owner lock.
`pg_foreground.py` retains a direct-child handle and checks the actual protocol
response names the expected data directory. Native `pg_ctl` immediate shutdown
owns PostgreSQL child cleanup. A failed stop preserves the scratch directory;
no custom family/cwd scan authorizes deleting it.

[[shutdown.meiva.okf.md|Backup job shutdown]]

## Process image and database admission

The scheduler captures its loaded image once for logging and health. Each
operation worker is a separate process: it captures its own image and shares
one gate between its log database, dump handle and restored-checkpoint reader.
The worker request carries no gate object. Scratch handles replace only the URL
in the live dial configuration; publication, direct owner dials and cleanup
retain their existing contracts.
