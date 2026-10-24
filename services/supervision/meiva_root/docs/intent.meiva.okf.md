---
type: doc
title: Meiva Root — unit intent, recorded failures and unattended stops
description: The policy fact each unit carries, who last set it, how a stored stop or failure survives a root restart, and when an operator stop stops being quiet.
tags: [services, lifecycle]
---

# Unit intent and recorded failures

Each unit carries one policy fact: its `intent` (`running` | `stopped`) and the
source that last set it (`operator` | `self` | `selection`), persisted per unit
under the run directory (`intent/<unit>.json`, atomic writes). Classification of
an expected stop reads only this intent — a mechanical transition residue never
reads back as an operator action (task #4872). An interrupted replacement is
recorded explicitly as `restart_failed` (the half it failed in, `down` or `up`,
and since when) and clears only once a fresh active generation proves it gone;
the health monitor may retry within its finite process-local budget. A root restart
merges each stored record conservatively: an explicit operator or selection stop
holds its unit down, a stop root recorded for itself during shutdown is
superseded by the admitted start, and a recorded failure is carried until a
generation proves it gone. A unit with no stored record — one adopted from a
generation that predates this store — or with an unreadable one (the store logs
it) takes the same default at its admitted start: `running`, source `selection`.
Residue from an older scheme is discarded, never consulted; a fleet update's
full stop/start leaves no mixed state.

Each record also carries the wall-clock time it was written and, when the caller
was readable, the caller facts that asked for it (argv, cwd, uid, pid, machine,
environment variable names). The caller is the only attribution an operator stop
leaves behind, so the store logs it at the write: the next write overwrites the
record. The write time makes a stop's age readable across root restarts — the
fact an unattended stop is reported from (task #5611).

## Launch rewriting

The assembly may rewrite a unit's launch argv; one that starts and then dies is
retired: [[services/supervision/meiva_root/docs/restart-policy.meiva.okf.md]].

## An unattended operator stop

An explicit operator stop is policy, not failure: it never counts toward the
restart budget and never signals while the operator still means it. What it may
not do is stay silent forever — on 2026-10-23 an `im-bridge` stop sat at DEBUG
for 14h12m and every alert behind it silently went nowhere (task #5611).

Once `HealthConfig.operator_stop_alarm_s` (default 6h) has passed since the
record was written, the round emits `root_unit_operator_stop_unattended` at
WARNING once for that record — unit, hours held, and the caller that asked for
the stop — then returns to DEBUG. Nothing restarts the unit: the finding is that
no owner came back for it. A later stop writes a new record and re-arms the
alarm; a live unit clears it.
