---
type: doc
title: Task Escalation — Delegator Digest and Human Notice
description: How task-maintenance escalates a repeat non-responder once reminders reach the threshold - the at-most-once-per-window digest to the task's manager (the nearest non-terminated ancestor owner), the require_response notice when no live manager exists, and the locking, idempotency and transaction rules that keep both paths safe under concurrent sweeps.
tags:
- fleet
- tasks
- reminders
---

# Task Escalation — Delegator Digest and Human Notice

## Responsibility

When a task owner keeps ignoring reminders, the daemon stops asking the owner and
tells someone who can act: the delegating parent owner, or the human queue when
there is no delegator. The sweep that scans, reminds and backs off is
[[meiva_builtins/plugins/meiva_fleet/task_maintenance/docs/task-maintenance/task-maintenance.meiva.okf.md|Task-Maintenance]]'s
to state; this node holds the escalation rules and their boundary cases. Like the
rest of the daemon it only sends messages and inserts notices, never modifies task
status.

## Escalate

- **Escalate**: fires when `reminder_count` reaches Fleet `task_escalate_n` (default 3). Two paths: **with a manager** (`routing.nearest_manager`: the owner of the nearest ancestor task that is not terminated — the direct parent's owner in the common case, so an agent terminated after delegating cannot swallow the digest) → per-owner digest chat to that manager; **without one** → INSERT a require_response `agent_notices` on the stuck owner (`task_id`=that task, `priority` inherited, "stuck, please reassign or cancel"). The notice attaches to the agent but **audience is human**: the human queue reads it, and the spine stages its `core.needs_input` delivery in the same transaction, so the stall also pings the user. The delegation path is **at most once per overdue window**: it fires at `>=` threshold while `escalated_at` is unset, and the delivered digest's own transaction stamps `escalated_at` (a failed delivery leaves it unset and the next sweep retries; a write that changes the work clears it with the reminder counters, re-arming the next window — a note-only write does not). The user path uses `>=` (an already-open notice **skips the round and retries next sweep**, so slipping past the threshold never permanently misses the window), and the sent notice itself is the idempotency marker, keeping "at most one open notice per agent". Candidate discovery does not authorize delivery: each batch locks current tasks and their discovered parents in stable id order with `FOR NO KEY UPDATE SKIP LOCKED`, then rechecks the task window and current parent owner. Locked or newly reparented tasks defer to a later sweep. All recipient identities are locked in stable id order; human notice open checks and local-id allocation share the gateway creator's physical `agents` lock. Digest insertion and escalation markers commit together in one batch transaction; a failure rolls the entire batch back. Live hints and `task_escalation` telemetry follow commit. Resolving a human notice still permits a later sweep to re-alert.

## Entry Points

- `meiva_builtins/plugins/meiva_fleet/task_maintenance/escalation.py` — accepts escalation from locked current state, before any live hints

Parent: [[meiva_builtins/plugins/meiva_fleet/task_maintenance/docs/task-maintenance/task-maintenance.meiva.okf.md|Task-Maintenance]].
