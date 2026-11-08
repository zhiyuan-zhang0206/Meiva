---
type: doc
title: Task Reminder Delivery — Digest, Reroute, Undelivered Count
description: Who a due task reminder reaches, and whether it reached anyone - the scan and the per-owner digest with its counters, the reroute to the nearest live ancestor when the owner is terminated, and the per-owner count that exposes a delivery path that stopped without leaving a counter behind.
tags:
- fleet
- tasks
- reminders
---

# Task Reminder Delivery — Digest, Reroute, Undelivered Count

## Responsibility

A reminder is worth anything only if it reaches someone who can act: the owner if it is
alive, its manager when it is not, and the human queue when no live agent stands above the
task. This node owns that path and the one reading that says whether it worked — delivery is
what advances the counters, so a path that stops leaves no counter to notice. The daemon's
scope is
[[meiva_builtins/plugins/meiva_fleet/task_maintenance/docs/task-maintenance/task-maintenance.meiva.okf.md|Task-Maintenance]]'s;
what happens once reminders accumulate is
[[meiva_builtins/plugins/meiva_fleet/task_maintenance/docs/task-maintenance/escalation.meiva.okf.md|Task Escalation]]'s.

## Remind

SELECT `status='in_progress' AND owner IS NOT NULL AND NOT is_root` and `now()-updated_at > remind_interval_seconds`, `ORDER BY priority, id` (P0 first); deliver one per-owner digest through a direct `inbound_messages` INSERT (`kind='system_note'`, `source='system'`), publish an owner invalidation hint after commit, then best-effort Redis-wake it. The digest copy is a check-in, not an overdue verdict (user correction 2026-10-17): it asks whether the task needs a progress update or a push forward, and to state why and adjust `remind_interval_seconds` when it cannot advance — an explicit wait instead of silent idling. The selected task rows are locked with `FOR UPDATE SKIP LOCKED`; digest insertion and `last_reminded_at=now()` / `reminder_count += 1` commit in the same transaction. Concurrent sweeps cannot accept the same cadence twice. A rollback preserves both queue and counters; a crash after commit leaves the pending digest recoverable by the existing inbound wake scan. Post-commit hint failure cannot re-arm that cadence. After commit, telemetry records `task_reminder_digest` (owner digest) or `task_reminder_rerouted` (manager digest / human notice); both are `persist` events, so `telemetry_events` holds them (the record the root `task-telemetry` diagnostic reads). `last_reminded_at` gates the backoff (Fleet `task_reminder_backoff_seconds`, default 3600s)—at most one reminder per backoff within the same overdue window (so the count can climb to the escalate threshold in one window).

## Reroute for terminated owners

A dead owner cannot act, so its due reminders are rerouted, never left in its queue. `routing.nearest_manager` names the manager — the nearest ancestor owner that is not terminated — and one digest per manager (grouped by dead owner) replaces the owner digest; with no live owner above, the human queue gets one notice on the dead owner. The reroute advances the same cadence and stamps `escalated_at`, so the escalate pass does not repeat it. Delivery never resurrects a terminated agent (2026-08-22); it stops addressing one (2026-10-25).

## Undelivered count

Delivery is what advances `reminder_count`, so a delivery path that stops leaves nothing to notice — the sweep sends nothing, and no counter moves (task #6399, the gap #8596 exposed). Each sweep also counts, per owner, the tasks that stayed overdue past `remind_interval_seconds` plus one sweep plus the longest digest window with `reminder_count` still zero, and emits `task_reminder_undelivered` (tier `anomaly`, payload `owner_id` + `count`) for every non-zero owner. Paused tasks, terminated owners (which are rerouted instead) and tasks waiting behind a live child are excluded, so the number reads zero while delivery works and turns non-zero the moment it stops. The read lives in `plugins/meiva_fleet/task_maintenance/undelivered.py`; the grace is derived at the call site from the sweep interval and the configured digest windows, never a constant.

## Entry Points

- `plugins/meiva_fleet/task_maintenance/routing.py` — `nearest_manager` (nearest non-terminated ancestor owner) and `stage_human_notice` (the human leg the reroute shares with escalation)
- `plugins/meiva_fleet/task_maintenance/undelivered.py` — `report_undelivered`, the per-owner count of overdue tasks that never had a reminder delivered
