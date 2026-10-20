---
type: doc
title: Fleet update window guarantees
description: What refuses, gates and surrounds the cli.fleet_update stops — preflight, the lease announcement, stored schedules, the silence and the log.
tags: [cli, updates, operations]
---

# Fleet update window guarantees

The promises `cli.fleet_update` makes around the stops themselves
([[cli/docs/fleet-update.meiva.okf.md]] owns the halves and their order): what
refuses the window, what gates it, what it silences, and what must not leak.
What stops and what starts is never decided here: every choice is the operator's
explicit argument, forwarded unchanged.
`run` executes both halves in one window, preserving these same gates and
closing the silence only after `up` passes all verification.

## Preflight

Each listed host is probed in its own shell before anything is touched. The
probe uses `printf` because zsh's and dash's `echo` reinterprets backslashes,
which once mangled a hold record carrying a quoted `$HOME`.

## The leases and the failed stop

Two facts are read over SSH before the window can hurt anything, and each has
its own node: the active impersonation leases the stops would end
([[cli/docs/fleet-update-window/lease-announcement.meiva.okf.md]]), and — only
after a stop fails — which hosts the window took down and must bring back
([[cli/docs/fleet-update-window/failed-stop-recovery.meiva.okf.md]]).

## The stored-schedule gate

A stored schedule script lives in the database, not the checkout: a library
change leaves it crash-looping the moment the schedule manager starts, and
the gateway's own `meiva schedules verify` only exists after that start. So
`down` checks first, the only code that may read the table being the home's
own OLD checkout, and the check being NEW's, offline, under a throwaway home;
both steps are real files whose text ships to the host, so their imports stay
visible.

## The silence

`down` opens one Grafana silence on the gateway host before the first stop
(comment naming NEW, expiry the required `--silence-hours`) and `up` expires it
after the drift checks. A silence that cannot open only warns; a failed `up`
leaves the silence to its expiry. `--dry-run` prints the command without
opening.

## The log and the macOS start

Every printed or logged line passes the secret redaction patterns first
(bearer tokens, secret-named assignments, URL passwords), and the log is
created 0600. The macOS one-shot writes its plist under a fresh 0700
directory in the remote `/tmp` and is left loaded past the start deadline,
reporting its label and log path; its label is its own
(`com.meiva.fleet-update.*`), never a registrar's job.

## Contracts

down refuses before touching anything — exit 2, no effects — when a listed host has uncommitted changes, a post-checkout hook, an active update marker or a maintenance hold that is neither free nor a completed stop, when the hosts disagree on HEAD, and when .python-version changes without --allow-python-change; the probe reads its facts through the host's own shell.

Before anything stops — under --dry-run too — down runs NEW's offline verify over the gateway's stored schedule table from a throwaway worktree: the read step runs on the home's OLD checkout by signature dispatch, the check goes through NEW's schedules CLI under a throwaway home, VERIFY_RC separates a crashed check from a red, a host that has not fetched NEW is reported as not checked, and a red or unevaluable table refuses with the rows listed unless --allow-red-schedules.

Before anything stops — under --dry-run too — down names the active impersonation leases and, after a failed stop, recovers the hosts that stop took down: [[cli/docs/fleet-update-window/lease-announcement.meiva.okf.md]], [[cli/docs/fleet-update-window/failed-stop-recovery.meiva.okf.md]].

down opens one Grafana silence on the gateway host before the first stop, its comment naming NEW and its expiry the required --silence-hours; up expires it after the drift checks; a silence that cannot open only warns and the half goes on; a failed up leaves the silence to its expiry; --dry-run prints the command without opening.

up starts every listed host even when one fails: a host whose start or converge aborts is reported with the step and reason recorded by that host's incomplete-converge marker ([[cli/commands/converge/docs/converge-incomplete.meiva.okf.md]]), the remaining hosts still start, and the run exits 3 (partial) after listing each failure — distinct from 2 (refused before any effect) and 1 (a failure before any host started). Roster, smoke and drift checks are skipped on a partial up; the silence still expires.

Parent: [[cli/docs/fleet-update.meiva.okf.md]].
