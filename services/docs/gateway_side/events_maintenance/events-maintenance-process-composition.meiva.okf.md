---
type: doc
title: Events Maintenance — process composition
description: How the maintenance daemon and its standalone rollup entry capture their code image, database gate and configuration owner, so a checkout move cannot relabel a running process.
tags: []
---

# Events Maintenance — process composition

Parent: [[services/docs/gateway_side/events_maintenance/events_maintenance.meiva.okf.md|events maintenance]].

The executable entry captures one `LoadedCommit` and retains its `CodeVersion`
and `ProcessDbGate`. The work database factory and the lazy telemetry producer
share that gate, so new handles preserve the process admission read budget.
Logging and the existing health endpoint receive the same captured image; a
checkout move cannot relabel the running daemon. The daemon's existing startup,
pidfile, work and bounded-exit order remains unchanged.

The standalone rollup entry captures its own image and binds an explicit,
non-exempt gate to `events_maintenance_db(config, gate=...)`. Argument rejection precedes
capture and database access; the closed-day SQL and fold-watermark rules remain
unchanged.

The service entry retains its prepared `ConfigBoot` and resolved unit runtime
path. Endpoint and pidfile operations borrow those inputs through
`ServiceEndpoints.from_boot`; each original lookup reads the same live view.
The work and logging database factory calls `db_config_from_boot` at each
handle creation and retains the entry's gate and lazy machine host reader.
Schema checks, health-before-pool ordering and shutdown stay at their original
boundaries. Business-specific configuration slices keep their existing readers.

The daemon's slice builder requires the same prepared `ConfigBoot` that its entry
passes to `run`. The Grafana-credential branch constructs its `EventBusConfig`
from that owner at the original conditional call point; units without the
credential do not construct this bus. The standalone rollup entry passes its
own existing boot owner to `events_maintenance_db`, whose live database slice is
read at that call. These inputs do not redeliver environment or timezone state.
