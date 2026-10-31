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

- `remind_interval_seconds: int | None` — seconds without activity on the task after which to remind the owner. The clock is `updated_at`, and a note moves it exactly like a change to the work does (see below). Default 1800 (30 minutes). **Reminders cannot be turned off**: passing `None` to `create` falls back to default, passing `None` to `update` means "no change". Maximum 24h (86400s). Can be `NULL` only on the [[task_model_root.meiva.okf.md|Root Task]] (which is never reminded).
- `last_reminded_at: datetime | None` — last reminder time; cleared when a write restarts the overdue window.
- `reminder_count: int` — number of reminders sent; cleared with the same rule.
- `reminders_paused_until: datetime | None` — an owner-set deadline that **silences** the task: the
  reminder and escalation passes skip it while the deadline is in the future, and a deadline at or
  before now means "not paused" (the column is the whole state, no second flag). Set through
  `update(reminders_paused_until=...)`, at most Fleet `task_reminder_pause_max_seconds` away; that
  policy is unset by default, and an unset policy means the home cannot pause at all — a request
  fails and names the key instead of a default nobody chose. Pausing is **not** progress: it zeroes
  no counter, clears no `escalated_at`, and does not move `updated_at`. The consequence is
  deliberate — when the deadline passes the task is due at once and its ladder continues from where
  it stood, so a task that had reached one reminder below `task_escalate_n` escalates on its first
  reminder after the pause. Parking is not forgetting. **The HTTP `PATCH` surface has no field for
  it**: the bound is this home's Fleet policy and one write boundary owns that check, so pausing is an
  owner action through the SDK.

A write clears `last_reminded_at`, `reminder_count` and `escalated_at` when it is **activity on the task**, restarting the overdue window from that write. Fleet `task_reminder_reset_on` (default `status`, `results`, `note`, `owner`, `priority`, `parent`) names the kinds that count: a change to the work, and a note. A note counts because it is the one signal that separates an owner still on the task from one that never came back — it did not before 2026-10-31, so an owner who answered a reminder with a `log` line was escalated as if the task had been silent. Renaming a task or editing its description still does not count: neither is activity on it. The comparison is against the stored row, so restating the current status or results is not a change either. `meiva_builtins/plugins/meiva_fleet/task_maintenance/daemon.py` sees the counter; `base/agents/tasks/reminder_window.py` owns the criterion. The same criterion moves `updated_at` — the column the daemon measures the overdue interval against — so the due clock and the ladder cannot disagree.

## Daemon Behavior

The task-maintenance daemon only sends reminders for tasks where **`status='in_progress'` AND `owner IS NOT NULL` AND `NOT is_root` AND no direct child is `in_progress`** (done and cancelled tasks are not reminded; the root task never gets reminded; a domain container is skipped while any of its direct children is open, because those children remind individually, and it becomes eligible again once they close). Post-2026-08-29 every task is born `in_progress`, so a newly created task is reminder-eligible immediately.

Within the same overdue window, at most one reminder per backoff period (Fleet `task_reminder_backoff_seconds`, default 3600s). A task whose `reminders_paused_until` is in the future is skipped by both passes. When `reminder_count` reaches Fleet `task_escalate_n` (default 3), escalation occurs:

- If the parent has an owner → notify the parent owner.
- If the parent has no owner (a top-level task under the unowned system root) → insert a `require_response` notice into the human queue.

Details in [[meiva_builtins/plugins/meiva_fleet/task_maintenance/docs/task-maintenance/task-maintenance.meiva.okf.md|Task-Maintenance]].
