---
type: doc
title: Task Model — Reminder Mechanism
description: "Reminder fields and daemon behavior: remind_interval_seconds, last_reminded_at, reminder_count, backoff, and escalation"
tags:
- fleet
- tasks
- data-model
- reminders
---

# Task Model — Reminder Mechanism

## Fields

- `remind_interval_seconds: int | None` — seconds without a change to the work after which to remind the owner. The clock is `updated_at`, so recording a note does not postpone the reminder. Default 1800 (30 minutes). **Reminders cannot be turned off**: passing `None` to `create` falls back to default, passing `None` to `update` means "no change". Maximum 24h (86400s). Can be `NULL` only on the [[task_model_root.meiva.okf.md|Root Task]] (which is never reminded).
- `last_reminded_at: datetime | None` — last reminder time; cleared when a write restarts the overdue window.
- `reminder_count: int` — number of reminders sent; cleared with the same rule.

A write clears `last_reminded_at`, `reminder_count` and `escalated_at` when it **changes the work**, restarting the overdue window from that write. Fleet `task_reminder_reset_on` (default `status`, `results`, `owner`, `priority`, `parent`) names the kinds that count; recording a note or renaming a task does not, because neither says anything about progress — resetting on the daily note stream is what held every task's counter at zero. The comparison is against the stored row, so restating the current status or results is not a change either. `meiva_builtins/plugins/meiva_fleet/task_maintenance/daemon.py` sees the counter; `base/agents/tasks/reminder_window.py` owns the criterion. The same criterion moves `updated_at` — the column the daemon measures the overdue interval against — so the due clock and the ladder cannot disagree.

## Daemon Behavior

The task-maintenance daemon only sends reminders for tasks where **`status='in_progress'` AND `owner IS NOT NULL` AND `NOT is_root` AND no direct child is `in_progress`** (done and cancelled tasks are not reminded; the root task never gets reminded; a domain container is skipped while any of its direct children is open, because those children remind individually, and it becomes eligible again once they close). Post-2026-08-29 every task is born `in_progress`, so a newly created task is reminder-eligible immediately.

Within the same overdue window, at most one reminder per backoff period (Fleet `task_reminder_backoff_seconds`, default 3600s). When `reminder_count` reaches Fleet `task_escalate_n` (default 3), escalation occurs:

- If the parent has an owner → notify the parent owner.
- If the parent has no owner (a top-level task under the unowned system root) → insert a `require_response` notice into the human queue.

Details in [[meiva_builtins/plugins/meiva_fleet/task_maintenance/docs/task-maintenance/task-maintenance.meiva.okf.md|Task-Maintenance]].
