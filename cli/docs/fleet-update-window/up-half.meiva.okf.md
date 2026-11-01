---
type: doc
title: Fleet update up half and completion gates
description: Ordered steps and completion invariants of cli.fleet_update up — host starts, roster convergence, runner smoke, package refresh, drift checks and silence expiry.
tags: [cli, updates, operations]
---

# Fleet update up half and completion gates

`cli.fleet_update up` brings a cluster back after `down` switched every checkout to `NEW` (either inside `cli.fleet_update run` or as a standalone half after manual release work or recovery).

## Steps, in order

1. `meiva start` with the operator's service selection on the gateway first, then across all runners in parallel; on macOS through a run-once `gui/<uid>` LaunchAgent, because the helper's launchd job lives in `gui/<uid>`. Each start must exit 0.
2. The roster gate: every listed machine online with checkout and running code on one commit; unlisted machines are reported and never block.
3. The gateway smoke-tests all listed serving agent-runners in parallel with real agents.
4. `meiva packages refresh` across all hosts in parallel; it never forces.
5. The drift checks, last, in parallel: `meiva schedules verify --no-notify` on the gateway and `meiva plugins verify` on every host. Every check runs to the end and a red fails `up` with all the details — after the smoke and the refresh, so a red never skips them.
6. Expire the silence.

## Completion contract

Before `up` completes: every listed machine is online on the roster with checkout and running code on one commit — re-read on the poll interval until `--roster-timeout`; a listed host is matched by the machine name it reports, a name with no row fails, unlisted machines are reported and never block; the gateway has smoke-tested each listed agent-runner; and every host ran `meiva packages refresh`, which replaces a differing local copy and never forces.

A failure ends the half: nothing rolls back, the message names the step to fix, and the whole half is rerun (`up` never repeats `down`). Refusals exit 2 (nothing touched); failures exit 1.

Parent: [[cli/docs/fleet-update.meiva.okf.md]].
