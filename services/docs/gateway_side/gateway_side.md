# Gateway-Side Services

## What is it
Groupings of background services running on machines whose capabilities set includes `gateway` (single machine with `gateway,agent-runner` or pure gateway). The gateway capability owns data-plane-adjacent daemons: the HTTP gateway itself, frontend, cluster-level wake-up daemons (heartbeat / task-maintenance only INSERT inbound + Redis best-effort publish to wake the owner; the agent's SELECT recheck guarantees delivery, so they belong to a single gateway, not every runner), memory/vector stack, all selected into the host root manifest.

`services/docs/gateway_side/` is a **capability grouping, not a directory of code** — there is no `services/gateway_side/` package. Each daemon's code lives in a domain group under `services/<group>/<name>/` (groups: `wake`, `upkeep`, `derived`, `entrypoints`, `backup`, ...); which side it runs on is a `ServiceSpec.capabilities` attribute, which cuts across the filesystem and cannot be expressed by co-location. This node and its children are the index layer for that attribute, the same way the domain roots under `docs/` are the index layer for the whole graph. Do not "fix" it by moving the capability split into the code tree — that deletes the only place the capability split is represented in the hierarchy.

## Service List
Source of truth = services in `build_services()` of `services/supervision/roster/composition.py` whose `ServiceSpec.capabilities` include `gateway` (the `GATEWAY` / `BOTH` groups), including plugin-declared entries folded in via `services.supervision.roster.plugin_services.plugin_services()` — this table is the **merged roster**'s gateway-side view.

| Service | Responsibility | File |
|---------|----------------|------|
| heartbeat | Idle agent heartbeat wake-up | [Heartbeat — Idle Agent Heartbeat](heartbeat.md) |
| delivery-watchdog | Stall alerting + stranded-owner wake fallback + stale-inbound dead-letter sweeps | [Delivery watchdog](delivery_watchdog/delivery_watchdog.md) |
| ttl-reaper | Wall-clock deadline enforcement: expired pages / persistent shells / browser sessions / impersonation leases, lifecycle-pointer scans, fire-log prune | [TTL Reaper — wall-clock deadline enforcement](ttl_reaper.md) |
| schedule-manager | Keeps one resident session alive per enabled schedule (reconcile + crash backoff/breaker, API sync requests) | [Schedule Manager — resident schedule sessions](schedule_manager.md) |
| insights | CPU-heavy read models over agent history (run timeline) on a Unix socket; the gateway authenticates and proxies | [Insights service — read models over agent history](../../derived/insights/README.md) |
| events-maintenance | unified `events` stream maintenance (immutable-Loki class resolution + day-grain rollup) | [Events Maintenance — cost-ledger rollup + class resolution](events_maintenance/events_maintenance.md) |
| memory-indexer | memory pool vector index | [Memory Indexer](memory_indexer/memory_indexer.md) |
| labeler | agent auto-naming | [Labeler — Agent Auto-Naming](labeler.md) |
| pg-backup | Daily local Postgres backup (scheduler daemon, ServiceSpec service) | [PG-Backup — Daily Local Postgres Backup](../../backup/artifact/docs/backup.md) |

## Also Owned by Gateway Capability, Documented Elsewhere
The following services also run on the gateway capability (core `GATEWAY` entries or plugin-folded-in entries), but their concept docs live in their own subtrees and are not repeated here:

- **gateway** — HTTP gateway itself (see [Gateway](../../../gateway/docs/gateway.md))
- **task-maintenance** — overdue task reminder + escalation. Registered by the `meiva_fleet` plugin via `meiva_builtins/plugins/meiva_fleet/services.py`, folded into the roster by `services/supervision/roster/plugin_services.py:plugin_services()`; runs on the gateway side, but **the node lives with its code in the plugin subtree** (see [meiva_fleet › Task Maintenance](../../../meiva_builtins/plugins/meiva_fleet/task_maintenance/docs/task_maintenance.md))
- **frontend** — Next.js frontend (`ui/web/` subtree)
- **gate** — the public fleet UI entry, an ordinary gateway `ServiceSpec` owned by root. It uses the `frontend` entry-port slot and proxies the Next.js app on the separate `app` slot. Root readiness checks `GET /__meiva/healthz` and the captured listener lineage; status shows Gate in the ordinary service table. Gate stops with its selected application tree during planned downtime. See [Gate authentication and maintenance projection](../../entrypoints/gate/docs/gate.md).

## Key Dependencies
- [Root deployment wiring](../../supervision/meiva_root_glue/docs/meiva_root_glue.md) — root probes and supervises the selected service manifest.
- [Background Services Overview](../services.md) — upper-level index of grouping and capability distribution
