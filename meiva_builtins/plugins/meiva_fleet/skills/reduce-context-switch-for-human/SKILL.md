---
name: reduce-context-switch-for-human
description: "Coordinates reporting for delegated Meiva work. Use when choosing queue versus push updates or protecting the user's attention during delegation."
---

# Reduce Context Switch for the Human

Every visible delivery costs the human attention, including a queued message
that creates an unread indicator. Apply the core user-attention test first;
queue versus push and recipient routing come after that decision.

Since 2026-09-20 this discipline is a platform default: every agent's system
prompt carries its interruption boundary, gated by
the Fleet image field `reduce_context_switch` (default on). Reporting cadence, aggregation, and delivery procedures live here
and load on demand. Read this skill as the full playbook behind those rules.

## Default: queue, never push

Use `meiva.ui.ask` for questions and the configured publish/text protocol for
results that pass the core user-attention test. Publications retain unread state,
and questions retain pending answers. Neither needs a separate notification call.
Delegated progress goes to the delegator through `send_message`. With no delegator,
deliver directly only when the information deserves the user's attention; record
other results without publishing them.

## Cadence is a delegation contract

These routing conventions apply to accepted delegated work. They do not require
creating a manager role or a delegation tree. They choose the recipient, not
whether a user-visible message is warranted.

Each meaningful publication appends history; each Ask preserves its own answer.
Never replace unread information with a rolling summary.

Peer reports are **milestone-based by default** (user ruling 2026-09-03):
report a useful milestone, blocker, completion or genuine request to the agent
responsible. These events do not automatically justify a user publication.
Preserve direct human answers, usable requested results, important new facts,
changed promises, adverse outcomes and corrections to conclusions the user relied
on. Resolved internal failures with unchanged goals and promises stay in truthful
records and owner reports. Deliver each substantive user result once, without
duplicate progress, receipt and final summaries. No daily or timed broadcast is
required. Never send routine progress or a bare acknowledgment. A delegator that wants a
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
- No task, no manager: apply the same user-attention test before direct delivery; do not manufacture a hierarchy to relay through.

## Emergency push

Push only for a true emergency: irreversible risk in motion, the whole effort blocked on the user, or the user explicitly asked to be woken for this. Everything else queues.

A push is out-of-band delivery to wherever the human actually is; find the channel yourself through an available, authorized channel. This skill sets when, not how.


## Checking instruction changes

The constructed paired cases in [evals/evals.json](evals/evals.json) and their
[metrics](evals/metrics.md) check both routine-noise suppression and important
result retention. Static prompt checks and manual scenario review establish
instruction coverage, not model adherence or production behavior rates.
