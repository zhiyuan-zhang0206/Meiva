---
type: doc
title: Lifecycle caller attribution
description: Who asked for a stop, start or restart — on the wire, in the root's intent record and in the audit stream.
tags: [cli, lifecycle, audit]
---

# Lifecycle caller attribution

The root supervisor used to record only *that* an operator stopped a unit
(`source: operator`), never *who*. Two incidents proved the gap: on 2026-10-15 a
recovery action had no recorded origin, and on 2026-10-18 19:24 a whole-tree
stop→start on ubuntu could only be attributed by cross-reading sshd logs and
crontab writes (task #5325).

## On the wire

A state-changing request (`up`, `down`, `force-down`, `restart`, `shutdown`)
carries an optional `caller` object; `parse_request` validates its shape
fail-fast and rejects it on every other verb:

    {"verb": "down", "name": "gateway", "caller": {
        "pid": 4821, "ppid": 4800, "uid": 501,
        "cwd": "/Users/x/.meiva/source",
        "argv": ["meiva", "stop", "-y"],
        "agent_id": "1818", "machine": "macmini",
        "operation_id": "stop@1792322999",
        "env_keys": ["DEEPSEEK_API_KEY"]}}

`caller_identity()` in `base/native_process/root_control/client.py` builds it:

- `argv` is truncated (32 items, 200 characters each) and **redacted** — a
  sensitive flag (`--token`, `--password`, `-p`, …) loses its value, whether it
  is separate or `--flag=value`.
- `env_keys` carries the **names** of credential-shaped variables that were set,
  never their values.
- `machine` is the kernel hostname, so two hosts cannot be confused. The
  configured cluster name would load `base.config` from a module it already
  reaches, closing an import cycle.
- `operation_id` is the `MEIVA_OPERATION_ID` the launcher exported (a local-pause
  or rollout operation), or absent when the caller is a bare command.

## In the root

`services/supervision/meiva_root/intent_store.py` persists the block as
`caller` in each unit's intent record, beside `intent`, `source` and
`restart_failed`. A record written before this field existed still reads (the
decode accepts the legacy field set), and an unreadable block reads as absent —
attribution is evidence, never a boot gate.

## The audit event

The intent record answers "what is this unit's state"; it cannot answer "who
stopped the whole cluster, when, from which host, with which command" without
reading every unit's file. So the requester records that fact itself, as an
`audit_event` named `lifecycle_request` (`base/events/declarations/root.py`):

- one row per **batch** — `meiva stop` records `down` (or `force-down` when it
  escalates) with every unit it is about to take down; `meiva start` records `up`
  with the units it is about to resume;
- written **before** the first request leaves the process, so a request that
  hangs, is refused, or outlives its caller still names the caller;
- `caller` carries the same block the wire carries — one fact, two readers;
- `source` is `agent:<N>` when `MEIVA_AGENT_ID` is set and `user` otherwise. A
  boot job's retried start is not an operator request and records nothing.

`cli/database/lifecycle_audit.py` owns the writer; the operator command
that holds the database scopes it with `lifecycle_audit(...)`. Recording goes
through `record_audit_reported`, not a standalone write: `audit_events` is the
system of record and the unified stream its projection, and a database that is
already down is exactly the state `meiva start` repairs — the record must never
fail the command that repairs it.

## Protocol compatibility

The `caller` field is **not** backward compatible. A root older than this change
rejects the whole request (`invalid_request: unknown request field(s): ['caller']`),
and the client surfaces it — so `meiva stop` fails against an old root, while
`meiva start` answers with a generation/digest mismatch and asks for a stop
first. The supported flow is unaffected: the fleet stops on the old checkout
before pulling. A bare `git pull` followed by `meiva restart` on a dev box is
not — stop with the old checkout, or kill the root, before pulling.

## Known boundaries

- A cold start (`meiva start` with no live root) boots the tree from the
  manifest without an `up` request per unit, so it records no
  `lifecycle_request` row. The record covers operator requests against a live
  root.
- `meiva stop` sends `down` with `force=True`, so its rows carry
  `verb="force-down"` while a restart's down leg carries `"down"`. The field is
  faithful to the wire verb; use `caller.argv` for the command that was run.
- `MEIVA_AGENT_ID` is self-declared and forgeable. The caller block is
  **evidence, not authentication**.

## What this does not do

It records, it does not authorize: the block is not authenticated beyond the
kernel-reported socket peer, and no permission model keys on it. The record is
evidence about the operation and never gates it — every failure in the audit
path is logged and dropped.

Parent: [[base/native_process/root_control/docs/root_control.meiva.okf.md]].
