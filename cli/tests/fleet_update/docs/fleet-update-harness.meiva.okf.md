---
type: doc
title: Fleet-update test harness
description: The in-memory cluster, the fixtures and the keep-decision stub the cli.fleet_update tests run against.
tags: [cli, updates, testing]
---

# Fleet-update test harness

`cli/tests/fleet_update/` proves `cli.fleet_update` without SSH, a shell or
launchd. `fleet_update_cluster.py` holds the in-memory `Cluster` each test
drives; `conftest.py` builds the shared `env` fixture on top — a real git
repository with an OLD and a NEW commit, `fleet_update.ssh` patched to the
cluster, `fleet_update.time` patched to its fake clock. `test_fleet_update.py`
covers the halves' behavior and the explicit-argument forwarding (the `run`
fixture supplies a default for every required choice, which a test overrides by
passing that choice), `test_fleet_update_leases.py` the lease announcement and its
confirm window, and `test_fleet_update_programs.py` runs the shipped
`cli/commands/management/fleet_gateway.py` program for real against a local
HTTP server standing in for the gateway API.

The cluster records every command and every effect it accepts, and the order
of those effects is what the tests assert. `hosts`, `names`, `silent`,
`hidden`, `roster_lag`, `stored_schedules`, `silence_reply`, `leases`,
`lease_reply` and `fail` are per-test knobs; the fake clock makes the roster's
poll interval and the lease countdown observable. `lease_reads` records how
many effects the cluster had already accepted at each lease read, so a test can
prove the leases are named before anything stops; a read is never an effect, so
`--dry-run` stays effect-free.

## Contracts

The in-memory cluster refuses production's forbidden order at the offending command — a gateway stop or start while a runner is up, a runner start before the gateway, a switch before its stop, a macOS start that is not the one-shot LaunchAgent — and the pre-verify simulation accepts only the gateway, only against NEW, with the table read before the worktree check and the check before any stop of the run.

Tests never run the real keep-decision script: the stub writes its payload into the fake checkout and appends each invocation's argv to a sibling file, so a test can tell whether the decision ran and over which revisions.

Parent: [[cli/docs/cli.meiva.okf.md]].
