---
type: doc
title: Boot autostart definitions and registration
description: Planned launchd values, bounded retry semantics and platform registration ownership.
tags: [base]
---

# Boot autostart

Boot-time autostart for a cluster's meiva-managed services.

Registers an OS job that runs `meiva start` on boot, so a machine reboot brings
the cluster's data plane + gateway / agents / daemons back up without a human
running `meiva start` by hand. Each cluster owns its Postgres+Redis under its
`$MEIVA_HOME`, brought up by `meiva start` itself (not a box-level brew/systemd
service), so this one boot job covers the whole cluster — data plane included.

- macOS: a launchd User LaunchAgent (RunAtLoad) in ~/Library/LaunchAgents/
- Linux: the enabled distro-level systemd unit (`base.host.system.boot_unit`);
  automatic startup requires systemd

The job **retries, boundedly** — one boot-time `meiva start` is not enough, because
at boot its dependencies are not all up yet, but an endless loop would hide a
permanent error. See `base/host/system/boot_policy.py` for the policy and for how
each mechanism states it: launchd keys plus the cap inside `meiva start --boot` on
macOS, systemd restart and start-limit keys on Linux.

Mirrors base/host/system/cron/mechanics.py (the health-probe registrar) -- same launchd / crontab
mechanics -- but fires at boot (RunAtLoad / systemd)
instead of on an interval, and runs `meiva start`.

Why the macOS path writes the plist but does NOT `launchctl bootstrap` it:
`bootstrap` on a RunAtLoad job runs it immediately, and this registrar is
invoked from converge, which runs *inside* `meiva start` -- bootstrapping here
would spawn a second, concurrent `meiva start`. launchd loads every plist in
~/Library/LaunchAgents/ at login, so dropping the file there is enough for it to
fire on the next boot (this box auto-logs-in). Enabling it in the current
session is a one-line manual step printed below, not something converge does.

Only ONE boot-ordering hazard is handled inside `meiva start` itself: a routable
bind address not up yet when this cluster's Postgres, PgBouncer or Linux Redis starts
(`cli/commands/data_plane/cluster_instance.py` waits for the reachable address before
binding Postgres; PgBouncer and authenticated Linux Redis use the same wait).
macOS Redis remains loopback-only. That covers a LOCAL address only. An enrolled agent-runner's
start also depends on a REMOTE gateway — its Settings build fetches
`GET /api/bootstrap` (`base.host.env.bootstrap`) — and on a VPN-joined runner that
interface comes up after the boot job fires. This module used to assert the
hazard class was already handled and emit a fire-once job; the retry above is
what actually covers it.

## Definition inputs and capture order

`boot_autostart/content.py` renders only the supplied planned argv, retry interval,
log path, environment home and PATH, using the shared `launchd_environment` encoder.
The log file is escaped as XML text in both output/error fields, so a private home
containing `&` or `<` round trips through plistlib without changing the real path.

The registration shell observes the plist destination, creates its directory,
resolves the binary, reads the log home, plans the boot arguments, reads the
environment home and then composes PATH, in that order. The two home reads and
binary observations remain independent; this is no shared or atomic home snapshot.
Only a changed definition is written. The enabled/home/selection gates and Linux
systemd delegation retain their original interfaces and order.

## Retry definition

The LaunchAgent plist: run `meiva start --boot` at load, and keep re-running
it while it fails, up to the boot attempt budget.

`KeepAlive` → `SuccessfulExit: false` is launchd's "restart in the inverse
condition" of a zero exit (launchd.plist(5)) — i.e. respawn only while the
job exits non-zero, and fall back to demand-based invocation the moment it
exits 0. `ThrottleInterval` overrides launchd's default 10-second respawn
floor, so a start that keeps failing is retried once a minute rather than
six times a minute. launchd has no attempt limit; `--boot` counts the
attempts and the one that spends the budget exits 0 to end the retries.
Together that is the retry policy in `base/host/system/boot_policy.py`, with
launchd rather than a loop of ours doing the retrying.

Start remains unsuccessful until readiness passes. Its idempotent root
reconciliation preserves healthy units on subsequent boot attempts.

## Validation boundary

Pure definition tests parse the actual XML. A fresh child writes only private
HOME/MEIVA_HOME files and uses the existing refusing CommandRunner resource:
private HOME alone does not isolate the OS scheduler. The tests check the written
argv, environment and retry values, both printed manual enable/reload forms,
unchanged inode/mtime for an identical definition, and a changed service selection.
They never execute launchctl bootstrap or install a native systemd unit.
