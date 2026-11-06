---
name: reduce-context-switch-for-human
description: "Coordinates reporting for delegated Meiva work. Use when choosing queue versus push updates or protecting the user's attention during delegation."
---

# Reduce Context Switch for the Human

Every push costs the human a context switch. When their attention is elsewhere, the default is silence: work lands in queues they drain on their own schedule.

Since 2026-09-20 this discipline is a platform default: every agent's system
prompt carries its interruption boundary and notice replacement semantics, gated by
the Fleet image field `reduce_context_switch` (default on). Reporting cadence, aggregation, and delivery procedures live here
and load on demand. Read this skill as the full playbook behind those rules.

## Default: queue, never push

Use `meiva.ui.ask` for questions and the configured publish/text protocol for
meaningful results. Publications retain unread state, and questions retain pending
answers. Neither needs a separate notification call. Delegated progress goes to
the delegator through `send_message`; deliver directly when no delegator is waiting.

## Cadence is a delegation contract

These routing conventions apply to accepted delegated work. They do not require
creating a manager role or a delegation tree; without a delegator, deliver to the
user directly.

Each meaningful publication appends history; each Ask preserves its own answer.
Never replace unread information with a rolling summary.

Updates are **milestone-based by default** (user ruling 2026-09-03): roll
up on a real milestone, a blocker, completion, or a real need — never
routine progress, never a bare acknowledgment. A delegator that wants a
different pattern names it in the brief; that is the exception, not a
per-delegation negotiation. The brief still carries the goal, boundaries,
budget, and any reporting exception.

## A direct reply is the user responding

Interpret direct messages in context. Preserve the original question and its
receipt; answering a question in the Ask UI settles it explicitly. Do not infer
approval from a read receipt, silence or a suggested Note.

## Roll-up bisection

- **Authorization / decision** — something only the user may decide — goes to the user directly, from any depth in the tree.
- **Progress / conclusions** go to your manager (the parent task's owner), who digests and aggregates before anything reaches the user's queue.
- No task, no manager: deliver directly; do not manufacture a hierarchy to relay through.

## Emergency push

Push only for a true emergency: irreversible risk in motion, the whole effort blocked on the user, or the user explicitly asked to be woken for this. Everything else queues.

A push is out-of-band delivery to wherever the human actually is; find the channel yourself through an available, authorized channel. This skill sets when, not how.
