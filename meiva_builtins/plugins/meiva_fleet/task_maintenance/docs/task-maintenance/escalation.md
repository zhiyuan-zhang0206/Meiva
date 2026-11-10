# Task Escalation — Delegator Digest

## Responsibility

When reminders accumulate and the task's state has not changed, the daemon stops
asking the owner and tells someone who can act: the task's manager. When there is no
live manager nobody can act, and nothing is sent. The copy states that evidence and nothing
more — see **Copy** below. The sweep that scans, reminds and backs off is
[Task-Maintenance](task-maintenance.md)'s
to state; this node holds the escalation rules and their boundary cases. Like the
rest of the daemon it only sends messages, never modifies task status.

## Escalate

- **Escalate**: fires when a task has gone Fleet `task_escalate_n` (default 3) of its own cadence periods (`max(task_reminder_backoff_seconds, remind_interval_seconds)`) without a state change, and at least one reminder has reached its owner. The threshold counts cadence silence, not delivered reminders: the per-owner digest window throttles delivery, so counting reminders would let the window stretch the wait. **With a manager** (`routing.nearest_manager`: the owner of the nearest ancestor task that is not terminated — the direct parent's owner in the common case, so an agent terminated after delegating cannot swallow the digest) → per-owner digest chat to that manager. **Without one** → the task is skipped: no message, no `agent_notices` row, no staged notification, no `escalated_at`; the pass logs the task ids at debug level and a later sweep finds the task again and does the same, without raising. The escalation is **at most once per overdue window**: it fires at `>=` threshold while `escalated_at` is unset, and the delivered digest's own transaction stamps `escalated_at` (a failed delivery leaves it unset and the next sweep retries; a write the policy counts clears it with the reminder counters, re-arming the next window — by default a note counts, so answering a reminder with a log line re-arms it). Candidate discovery does not authorize delivery: each batch locks current tasks and their discovered parents in stable id order with `FOR NO KEY UPDATE SKIP LOCKED`, then rechecks the task window and current parent owner. Locked or newly reparented tasks defer to a later sweep. All recipient identities are locked in stable id order. Digest insertion and escalation markers commit together in one batch transaction; a failure rolls the entire batch back. Live hints and `task_escalation` telemetry (`leg` is always `delegator`) follow commit.

## Copy

The digest describes the task, not the owner. The evidence this pass holds is
`reminder_count`, the task's state (nothing has been recorded on it since the
reminders began) and the ancestor owners; a note restarts the window exactly like a
change to the work does, so consecutive reminders mean the task saw neither. An
owner that answered in chat, worked in a shell or advanced a peer's task is
indistinguishable here from one that is gone, and the same words have to serve both — so the
message may not describe the owner's condition at all; it states the reminders and the
unchanged state, and ends in an action. Liveness does exist in `agents_meta` and
`routing.nearest_manager` reads it, but only to choose a recipient: it is not a
claim about the owner (2026-10-30 ruling, task #5862). The reminder digest made
the same correction earlier (user ruling 2026-10-17: a check-in, not an overdue
verdict).

## Entry Points

- `meiva_builtins/plugins/meiva_fleet/task_maintenance/escalation.py` — accepts escalation from locked current state, before any live hints

Parent: [Task-Maintenance](task-maintenance.md).
