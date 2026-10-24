---
type: doc
title: Task-Maintenance — Task Reminders + Escalation
description: A gateway-owned task reminder daemon — every 5 minutes scans in-progress tasks that are overdue for updates, queues a system-note reminder to the owner; after accumulation reaches a threshold, escalates to the parent task owner, or if parent owner is empty (top-level task under root), inserts a require_response notice into the human queue instead. Only speaks, never modifies task status.
tags: []
---

# Task-Maintenance — Task Reminders + Escalation

## What It Is
A gateway-owned daemon — every Fleet `task_maintenance_interval_seconds` (default 5 min) scans `agent_tasks`, chat-reminds owners of in-progress tasks overdue for updates, and escalates repeat non-responders: with a delegator (parent task has an owner) notify the parent owner; top-level tasks (parent = unowned system root) escalate to the human queue via a require_response notice on the stuck owner. Cluster-level, one per cluster, same shape as heartbeat. **Only sends messages / inserts notices, never modifies task status**—no stale sweep, no auto-cancel, no orphan release.

**Attribution**: fleet domain service under the `meiva_fleet` plugin namespace (not hardcoded in core `services/supervision/roster/spec.py`): the plugin declares a `ServiceSpec` via `meiva_builtins/plugins/meiva_fleet/services.py:services()`, `services/supervision/roster/plugin_services.py:plugin_services()` discovers it into the single-source `build_services()` roster—"plugin declares, ops discovers"; gateway capability. **Discovery keys on the plugin code's existence, not the agent-side enable-state**; the host gate is Fleet image field `task_maintenance_enabled` (via `ServiceSpec.gate`, evaluated at daemon-start, unaffected by per-agent overlays).

## Core Responsibilities
- **Remind**: SELECT `status='in_progress' AND owner IS NOT NULL AND NOT is_root` and `now()-updated_at > remind_interval_seconds`, `ORDER BY priority, id` (P0 first); deliver one per-owner digest through a direct `inbound_messages` INSERT (`kind='system_note'`, `source='system'`), publish an owner invalidation hint after commit, then best-effort Redis-wake it. The digest copy is a check-in, not an overdue verdict (user correction 2026-10-17): it asks whether the task needs a progress update or a push forward, and to state why and adjust `remind_interval_seconds` when it cannot advance — an explicit wait instead of silent idling. The selected task rows are locked with `FOR UPDATE SKIP LOCKED`; digest insertion and `last_reminded_at=now()` / `reminder_count += 1` commit in the same transaction. Concurrent sweeps cannot accept the same cadence twice. A rollback preserves both queue and counters; a crash after commit leaves the pending digest recoverable by the existing inbound wake scan. Post-commit hint failure cannot re-arm that cadence. After commit, and telemetry records `task_reminder_digest`. `last_reminded_at` gates the backoff (Fleet `task_reminder_backoff_seconds`, default 3600s)—at most one reminder per backoff within the same overdue window (so the count can climb to the escalate threshold in one window).
- **Escalate**: fires when `reminder_count` reaches Fleet `task_escalate_n` (default 3). **With a delegator** (parent owner non-null) a per-owner digest chat goes to the parent owner, at most once per overdue window; **without one** a require_response `agent_notices` row is INSERTed on the stuck owner, whose audience is the human queue (the spine stages its `core.needs_input` delivery in the same transaction). Each batch locks and rechecks current state, and digest insertion and escalation markers commit together. The idempotency, locking and re-alert rules are in [[meiva_builtins/plugins/meiva_fleet/task_maintenance/docs/task-maintenance/escalation.meiva.okf.md|Task Escalation]].
- **No resurrection for terminated owners**: direct delivery leaves their system `inbound_messages` rows unclaimed and only attempts the best-effort Redis wake; it never resurrects the terminated owner. The task instead escalates for reassignment or cancellation (2026-08-22 user ruling).
- **Schema drift suicide**: `psycopg.ProgrammingError` (code↔DB drift) directly exits, relying on healthcheck to restart, not retrying to mask the issue.

## Key Dependencies
- [[agent/db/docs/db.meiva.okf.md]] — reads `agent_tasks` (SELECT overdue tasks, JOIN parent for `parent_owner` + task `priority`) + writes `last_reminded_at`/`reminder_count`/`escalated_at` bookkeeping + direct `inbound_messages` INSERT (`kind='system_note'`, `source='system'`) + user escalation path writes `agent_notices` (require_response, then `publish_agent_updated_sync` invalidates the live view after commit, and the spine stages the `core.needs_input` delivery in that transaction)
- [[gateway/docs/gateway.meiva.okf.md]] — direct delivery refreshes the owner badge and only best-effort wakes Redis, never auto-resurrects
- [[loop.meiva.okf.md]] — reminder/escalation messages are received and processed by the owner/parent owner on their respective machines

## Entry Points
- `meiva_builtins/plugins/meiva_fleet/services.py:services()` — declares this service's `ServiceSpec` (session `task-maintenance`, gateway capability, carries the gate and its captured Fleet config)
- `plugins/meiva_fleet/task_maintenance/daemon.py` — `.venv/bin/python -m plugins.meiva_fleet.task_maintenance.daemon`
- Health: the standard `/healthz` identity probe on :8108, built by `services/supervision/roster/healthz.py:healthz_daemon` and centrally bound to captured root ancestry (no module of its own)

## Notes
- Entry captures one `LoadedCommit` for logging, health and the `CodeVersion`
  behind its non-exempt `ProcessDbGate`. Logging and `run(config, database=...,
  bus=..., image=..., endpoint=...)` share one live-settings database factory;
  `main()` is the composition root that builds the bus and reads the daemon's
  `ServiceEndpoint` (health port and pidfile) once, so `run` takes them as
  values and holds no default dependency. Unknown image stays unknown. The
  original Fleet service packet/disk admission, schema check, pidfile,
  health-before-pool startup and pool/health/pidfile cleanup remain; `run`
  alone removes the pidfile, and only after it claimed it, so a refused start
  never deletes the running daemon's pidfile. The tests run `run` against a
  pidfile and port of their own, the throwaway Postgres and Redis, and a
  separate process that holds the pidfile; `main()` runs as a child process
  against the same resources.
- Fleet owns the host gate (default on) and cluster-pinned interval/backoff/escalation policy in `default_config.py`. Roster composition captures one typed image for the gate and daemon launch; its canonical non-secret packet is included in `unit.env` and the existing generation digest. Later image edits do not change that running generation. Core retains health port/pidfile and notification TTL responsibilities.
- The plugin ServiceSpec selects task-maintenance into the gateway root manifest. Root health probes that exact unit and owns verified recovery; the plugin probe has no independent launch authority.
- Difference from [[services/docs/gateway_side/heartbeat.meiva.okf.md]]: heartbeat wakes idle agents to work, task-maintenance reminds overdue tasks; both only INSERT inbound, both are one per cluster on gateway.
