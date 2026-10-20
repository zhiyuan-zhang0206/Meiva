---
type: doc
title: The machines table
description: 'The cluster roster row: composed `gateway_url`, the stop and maintenance-window stamps, and the spawn-target capability invariant.'
tags:
- base
- cluster
- machines
---

# The `machines` table

The cluster-wide roster. `gateway_url` holds whatever address the cluster dials
this host at, resolved once by `base.cluster.machines.unit_dial_url(roles)`: an
**agent-runner-capable row carries its ops server URL** (`http://localhost:<ops_port>`
co-located, `http://<reachable-host>:<ops_port>` split) — that is the dial target
spawn/lifecycle forwarding POSTs to; a gateway-only row carries its own gateway URL,
informational.

Two processes write that row, both through `register_self()`: `meiva start` at the
tail of a supervised bring-up, and the unit's **ops daemon at its own boot**. The
second is what makes `stopped_at` and `up_since_at` mean what their readers assume
— a host brought back by an autostart or a watchdog respawn un-stops itself instead
of serving ops under a stale `stopped` marker. `up_since_at` is a boot/announce
stamp, not a heartbeat — the name it earned in #981, after `last_seen_at` had spent
its life promising a heartbeat these tables have never had; liveness is the live
`status_probe`
([why](../../../../docs/decisions/runtime/processes/health/2026-07-29-liveness-is-written-by-the-live-process.md)).

A stop can declare how long the host is expected to be away. `meiva stop
--maintenance-for 45m --reason "..."` passes `maintenance_for_seconds` /
`maintenance_reason` to `POST /api/cluster/stopping`; `base.cluster.machines.mark_stopping`
writes them to the host row (`maintenance_until` = now + the window,
`maintenance_reason` = the operator's sentence) after recomputing the composed
row, and only when the host is now fully stopped — one co-located unit stopping
while its peer still serves leaves no window. They are HOST-level facts, unlike
the per-unit `stopped_at` announce: an ordinary stop clears whatever a previous
one declared, and `register_self()` clears them on the next start. The
liveness pass reads the row (`services/wake/heartbeat/liveness.py`) and reports
a host still down past `MACHINE_STOPPED_TOO_LONG_S` with no window ahead, so a
forgotten stop is visible without making a planned one look like a fault
(task #5464). `stopped_at` itself is stamped with `COALESCE(stopped_at, NOW())`:
a later recompute keeps the first stop's clock instead of moving the "how long
stopped" origin forward on every pass.

Cross-machine spawn is a direct dial, not a queue: after committing the row and
first prompt, the gateway POSTs a `spawn-launch` op to the target runner's ops
server (address read from this table), which calls
`ops/lifecycle/__init__.py:launch_agent_op` in-process and returns the result in
the same response. Runners run **no** local gateway process — an agent's SDK
reaches the gateway over HTTP via `gateway_api_base(gateway_url)`.

**Spawn-target invariant**: `POST /api/keyed/v1/agents` returns **HTTP 400** when the
resolved `machine` lacks the `agent-runner` capability (`'agent-runner' =
ANY(role)` is false) — typically a pure gateway node. A single box satisfies it,
so a local spawn there is allowed. The guard exists to catch "the UI booted an
agent onto a gateway-only node where only the scheduling/DB layer lives".
