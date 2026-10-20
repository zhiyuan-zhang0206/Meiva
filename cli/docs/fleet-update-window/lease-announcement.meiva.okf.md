---
type: doc
title: The lease announcement
description: Why a fleet update names the active impersonation leases before it stops the cluster, and what its confirm window does.
tags: [cli, updates, operations]
---

# The lease announcement

The stops end every external takeover that is running: a host or native restart
never adopts a relay across incarnations, so a lease that is active when the
cluster stops is aborted mid-task and its executor loses control
([agent impersonation hosts](../../../docs/conventions/agents/agent-impersonation-hosts.md)).
The rule therefore has a carrier, not only a sentence in a document: `down`
reads `agent_impersonations` rows with `status='active'` on the gateway host
before anything stops — under `--dry-run` too, in the same words — and names
each lease's agent, session, executor and expiry, followed by one warning that
the stop ends them. `cli/commands/management/fleet_leases.py` is a real file
whose text ships to the host, like the silence and roster programs, so its
imports stay visible to the reader and the first update that carries it already
has it. The filter is the status the table holds rather than the clock: a
stopped cluster's reaper lags, and the row still marked active is exactly the
one the stop would end.

A lease list that cannot be read only warns — the shipped program exiting
non-zero is the same warning, not a refusal — because an unreadable list must
not strand a window. "No active lease" and "not read" are separate lines and
cannot be confused.

The required `--lease-confirm-seconds` then holds the stops open for that long
with a printed countdown, so the operator can release or renew a lease first.
0 waits for nobody: the leases are still named before the first stop; a wave
that must not end a takeover passes a longer window. `--dry-run` reports
the window and does not sleep.

## Contract

Before anything stops — under --dry-run too, in the same words — down reads the gateway's agent_impersonations rows with status='active' and names each lease by agent, session, executor and expiry, followed by one warning that the stops end them; a read that fails — an unreadable table or a program that exits non-zero — only warns and the window goes on; the required --lease-confirm-seconds (0 reports only) holds the stops open that long with a countdown, and dry-run prints that window without sleeping.

Parent: [[cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md]].
