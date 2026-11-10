# Task-Maintenance — Task Reminders + Escalation

## What It Is
A gateway-owned daemon — every Fleet `task_maintenance_interval_seconds` (default 5 min) scans `agent_tasks`, chat-reminds owners of in-progress tasks overdue for updates, and escalates tasks that stay overdue to the nearest live ancestor owner; a top-level task with no live manager escalates to no one. Cluster-level, one per cluster, same shape as heartbeat. **Only sends messages, never modifies task status**—no stale sweep, no auto-cancel, no orphan release.

**Attribution**: fleet domain service under the `meiva_fleet` plugin namespace (not hardcoded in core `services/supervision/roster/spec.py`): the plugin declares a `ServiceSpec` via `meiva_builtins/plugins/meiva_fleet/services.py:services()`, `services/supervision/roster/plugin_services.py:plugin_services()` discovers it into the single-source `build_services()` roster—"plugin declares, ops discovers"; gateway capability. **Discovery keys on the plugin code's existence, not agent-side enable-state**; the host gate is Fleet image field `task_maintenance_enabled` (via `ServiceSpec.gate`, evaluated at daemon-start, unaffected by per-agent overlays).

## Core Responsibilities
- **Remind**: scans in-progress, non-root tasks overdue for an update and delivers one per-owner digest, inserting it into `inbound_messages` and advancing `last_reminded_at` / `reminder_count` in the same transaction. The selection, the digest window, the counters and the delivery-side reads are [Task Reminder Delivery](delivery.md)'s to state.
- **Escalate**: fires after `task_escalate_n` (default 3) cadence periods of silence. **With a manager** (the nearest ancestor owner that is not terminated — the parent's owner in the common case) a per-owner digest chat goes to that manager, at most once per overdue window; **without one** the task is skipped and nothing is sent. Each batch locks and rechecks current state, and digest insertion and escalation markers commit together. The idempotency, locking and re-alert rules are in [Task Escalation](escalation.md).
- **Reroute for terminated owners**: a dead owner cannot act, so its due reminders go to the nearest live ancestor owner instead, else nowhere; delivery never resurrects or addresses a terminated agent. [Task Reminder Delivery](delivery.md) owns the walk.
- **Undelivered count**: delivery is what advances `reminder_count`, so a delivery path that stops leaves nothing to notice — no message, and no counter moving (task #6399). Each sweep reports, per owner, the tasks past their interval plus one sweep plus the longest digest hold with `reminder_count` still zero. [Task Reminder Delivery](delivery.md) owns the criterion.
- **Schema drift suicide**: `psycopg.ProgrammingError` (code↔DB drift) directly exits, relying on healthcheck to restart, not retrying to mask the issue.

## Key Dependencies
- [Database Layer](../../../../../../agent/db/docs/db.md) — reads `agent_tasks` (SELECT overdue tasks, JOIN parent for `parent_owner` + task `priority`) + writes `last_reminded_at`/`reminder_count`/`escalated_at` bookkeeping + direct `inbound_messages` INSERT (`kind='system_note'`, `source='system'`)
- [Gateway](../../../../../../gateway/docs/gateway.md) — direct delivery refreshes the owner badge and only best-effort wakes Redis, never auto-resurrects
- [Agent Turn Loop](../../../../../../agent/docs/loop.md) — reminder/escalation messages are received and processed by the owner/parent owner on their respective machines

## Entry Points
- `plugins/meiva_fleet/task_maintenance/routing.py` — `nearest_manager` (nearest non-terminated ancestor owner)
- `plugins/meiva_fleet/task_maintenance/undelivered.py` — `report_undelivered`, the per-owner count of overdue tasks that never had a reminder delivered
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
- Difference from [Heartbeat — Idle Agent Heartbeat](../../../../../../services/docs/gateway_side/heartbeat.md): heartbeat wakes idle agents to work, task-maintenance reminds overdue tasks; both only INSERT inbound, both are one per cluster on gateway.
