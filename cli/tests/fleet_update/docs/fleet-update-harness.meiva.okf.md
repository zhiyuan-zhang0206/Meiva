---
type: doc
title: Fleet-update test harness
description: The fake ssh program, the cluster state it plays, and the fixtures the cli.fleet_update tests run against.
tags: [cli, updates, testing]
---

# Fleet-update test harness

`cli/tests/fleet_update/` proves `cli.fleet_update` without a second machine, but with every
process boundary real. The window under test is `fleet_update.main`, and the `ssh` it launches is
a program: `fake_ssh.py`, put first on the `PATH` the window is started with. The window finds it
the way it finds the operator's `ssh`, passes it the host alias and the command as argv and the
shipped program on stdin, and reads its stdout lines and exit status back.

`fleet_update_cluster.py` holds the `Cluster` each test drives. It stages a cluster as plain
attributes, writes them to a JSON state file before a run, and reads the state back after it; the
fake `ssh` loads that file once per command, applies the command the way a host would, records the
effects it accepted and the commands it saw, and saves the state. `conftest.py` builds the shared
`env` fixture on top: a real git repository with an OLD and a NEW commit, and a `run` function that
supplies a default for every required choice (a test overrides one by passing that choice).
`Cluster.invoke` stamps each invocation one second later than the last, so two windows in one test
never collide on a log name, and fails the test on any order production forbids.

`test_fleet_update.py` covers the halves' behavior, `test_fleet_update_choices.py` the
explicit-argument forwarding and refusals, `test_fleet_update_leases.py` the lease announcement and
its confirm window, `test_fleet_update_recovery.py` the failed-stop recovery,
`test_fleet_update_helper_gate.py` the helper gate, and `test_fleet_update_programs.py` runs the
shipped `cli/commands/management/fleet_gateway.py` program for real against a local HTTP server
standing in for the gateway API. `plan/` holds the tests of the strict decision modules: their
inputs are the text a host printed and their outputs are values, so they need no host at all.
`local_ssh.py` is the other `ssh`: it ignores the alias and runs the command in a real `zsh` or
`sh`, for the tests that check what a host's own shell does with a window's command.

The waits are real but short. `Cluster.pacing` hands the window tiny roster-poll and lease-tick
intervals, and the tests that exercise a wait pass the smallest whole-second timeout the command
accepts (`--roster-timeout 1`, `--lease-confirm-seconds 1`).

## Contracts

The fake `ssh` refuses production's forbidden order at the offending command — a gateway stop or start while a runner is up, a runner start before the gateway, a switch before its stop, a macOS start that is not the one-shot LaunchAgent — and its pre-verify simulation accepts only the gateway, only against NEW, with the table read before the worktree check and the check before any stop of the run; each refusal is recorded as a violation the test reads back and fails on.

`hosts`, `names`, `silent`, `hidden`, `roster_lag`, `stored_schedules`, `silence_reply`, `leases`, `lease_reply` and `fail` are per-test knobs. `lease_reads` records how many effects the cluster had already accepted at each lease read, so a test can prove the leases are named before anything stops; a read is never an effect, so `--dry-run` stays effect-free.

Parent: [[cli/docs/cli.meiva.okf.md]].
