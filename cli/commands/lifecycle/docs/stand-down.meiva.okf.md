---
type: doc
title: Stand-down
description: The record `meiva stop` leaves so the OS boot job cannot undo it — who writes it, who reads it, and when an explicit start ends it.
tags:
- cli
- lifecycle
---

# Stand-down

## What it is

`meiva stop` leaves a home stopped on purpose. Two pieces of machinery exist to
survive an *accidental* death and would otherwise undo that: the macOS
permissions helper's root keeper (it restarts a root that exited) and the host's
OS boot job (`com.meiva.autostart` on macOS, the unit's boot service elsewhere),
which runs `meiva start --boot` at login.

The keeper reads its own marker (`<home>/run/meiva-root/root-stopped`, written by
the helper's `stop_root`). `cli/commands/lifecycle/stand_down.py` writes the
record that explains that stop to the boot job:
`<home>/run/meiva-root/stand-down.json` — `{since, until, reason, machine}`,
mode `0600`, written durably before any teardown starts so a host that dies
mid-stop still reads as stopped on the next boot.

## The rule

**A stop says stopped and only an explicit start ends it.**

| caller | `until` | what happens |
|---|---|---|
| `meiva stop --maintenance-for T` | `now + T` | the boot job stays down until `T`, then may resume |
| `meiva stop` | `null` | the boot job stays down until someone runs `meiva start` |
| `meiva start` (operator) | — | clears the record and starts |
| `meiva start --boot` (OS job) | — | reads the record: blocked → prints one line and exits 0 without starting; blocked with `until` passed → starts |

A record that cannot be read or parsed counts as stopped, not as permission to
start: the same rule the helper applies to its own marker.

A stop nobody comes back for is caught by the existing `machine_stopped_too_long`
alert (20 minutes, one liveness pass every 30s) — the stand-down needs no timer
of its own (task #5493).

## Where it is written

`announce_stopping` in `_temporary_stop.py` writes the record beside its
best-effort `POST /api/cluster/stopping`: the database columns are the cluster's
view, and this file is the host's own — written even when the gateway is
unreachable, because the stop is happening either way.

Parent: [[cli/commands/lifecycle/docs/lifecycle.meiva.okf.md]].
