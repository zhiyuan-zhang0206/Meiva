---
type: doc
title: Stopped-too-long signal
description: How the liveness pass reports a host that an announced `meiva stop` left down past its window, and what stays silent.
tags:
- services
- heartbeat
- liveness
---

# Stopped-too-long signal

Each liveness pass reads the machines table for a host whose announced `meiva
stop` (`stopped_at`) is older than `MACHINE_STOPPED_TOO_LONG_S` (20 minutes — a
four-host sequential restart measured 14 on 2026-10-20) with no
`maintenance_until` still ahead, and emits one `machine_stopped_too_long` event
per such host; `stopped_for_s` carries the age. The
`meiva-ops-machine-stopped-too-long` Grafana rule grades it per machine.

The gap it closes is real: an intentionally stopped host leaves the probe target
set, so the machine-offline rules cannot see it. The 2026-10-14 stack sat stopped
for 53 minutes with nothing reporting it (task #5464).

Silent by construction:

- a host that never announced a stop (`stopped_at IS NULL`) — that is a
  different gap (never started or crashed), owned by the probe rules;
- a paused host (`meiva cluster pause`) — deliberately away, not probed;
- a staging host — not in the expected-up set;
- a host inside a declared window, until the window expires;
- a whole-stack stop, where this pass cannot run at all — the cluster-external
  probe owns that shape.
