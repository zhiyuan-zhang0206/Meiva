---
type: doc
title: Liveness probe inputs
description: Required probe configuration, RPC ownership and committed live announcements.
tags:
- heartbeat
- lifecycle
---

# Liveness probe inputs

A liveness pass probes every rollout target and roster-visible agent runner,
records host reachability and status snapshots, then merges agent liveness.
Probe failures are per-machine: a host being offline must not prevent healthy
hosts from being observed. Unexpected probe exceptions still propagate; only
the existing unreachable-operation outcome counts as a failed probe.

The pass requires its RPC callable and a frozen `LivenessConfig`. The daemon's
`AgentLivenessRound` composes the actual cluster RPC with its retained lazy
database and reads `settings.gateway.status_probe_timeout_seconds` for each
round. This preserves live changes between rounds. All probes in that fan-out
receive the same pass configuration. The setting still shares its owner with
the roster probe; it is not replaced by a second timeout default.

The configured timeout originally widened the historical three-second budget:
a healthy status snapshot could take just over three seconds, while an offline
host still refuses quickly. `probe_machine` receives the timeout through the
pass instead of reading process globals itself.

Machine observations and agent liveness remain durable PostgreSQL state. Only
a changed merged liveness edge emits the existing typed `AgentUpdated` hint,
after `merge_liveness` has committed. Repeated online/offline observations do
not create duplicate hints. Publication uses the existing required EventBus;
there is no optional publisher callback.

Tests provide the explicit pass configuration and probe. The edge-observation
client overrides the existing bus publish operation, parses the actual typed
payload and reads state through an independent database connection during the
announcement. Seeing offline and then online at that moment proves the hints
follow the committed transitions. Root TaskGroup failure/cancellation and pool,
health and pidfile cleanup remain the daemon's existing composition contract.
