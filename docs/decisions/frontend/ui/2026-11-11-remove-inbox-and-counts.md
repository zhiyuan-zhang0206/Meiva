# Remove the Inbox page and every aggregate attention count

## Context

The user-facing model had grown three overlapping surfaces: a Fleet "Inbox" page that listed every
open notice grouped by task with priority, blocking and resolved history; a summed red dot in the
sidebar header (`notification.awaiting_reply`); and per-agent counters on the roster
(`awaiting_response_count`, `highest_notice_priority`, `unread_notice_count`). The notices that gave
these meaning (progress notes, FYIs, escalations) were already gone: #8795 removed the agent-callable
`notify`/`edit_notice`/`dismiss_notice` entries, the gateway routes that created or edited a notice and
the task-maintenance escalation, the last non-question writer. Only `meiva.ui.ask` still writes
`agent_notices`, always as priority P2, with no expiry and never blocking. #8793 added the other half
of the model: one seen position per agent and an orange dot for what lies past it.

## Decision

The user's communication model has three things and nothing else:

1. what was delivered to the user (a reply with a display prefix; `meiva.ui.publish` in SDK mode);
2. the questions agents ask with `meiva.ui.ask`, shown as a question mark on the agent's sidebar row
   (highest priority) and in the question bar above that agent's composer;
3. one seen position per agent, and an orange dot when a delivery lies past it.

There is no "waiting for a reply that is not a question" state and no count. Consequences:

- The Fleet Inbox page, its task-subtree grouping, the resolved-history page of `GET /api/notices`,
  the Fleet tab and its badge, the collapsed-queue preference, the summed header dot and its setting
  `notification.awaiting_reply` are deleted. Fleet keeps the agent graph and the task graph.
- The roster and snapshot lose `awaiting_response_count`, `highest_notice_priority`,
  `unread_notice_count` and the 30-day FYI window. `has_pending_question` stays: an unresolved row with a
  question.
- Question cards no longer show a priority badge, `blocking` or an expiry, and are not sorted by
  priority. The task priority palette is kept, for tasks, in `lib/notifications/task-priority.ts`.
- `GET /api/notices/live`, the `notice` field of the inspector's live read and the TTL reaper's
  notice-expiry phase (with its owner message) are deleted: nothing writes `expire_at` any more.
- A data migration resolves, as `expired`, every still-open row that is not a question or that carries an
  expiry, so no legacy row lingers in the question reads. Columns, indexes and tables stay until a later
  contract step after this release is deployed (expand-contract).
- `meiva notices list/resolve/clear` keep their names (they operate on questions) and lose the
  priority and type filters.

## Alternatives rejected

- Keep the Inbox as a list of questions only. Every question already shows where the user answers it, in
  its agent's conversation; a second list beside the graph adds a place to look and, with the counts, a
  number that can disagree with the rows.
- Keep a count on the sidebar. A count invites triage by number; the dot and the question mark say what
  needs the user without ranking it.

## Supersedes

Narrows [unify agent notices](../../agents/messages/2026-06-19-unify-agent-notices.md) (the FYI feed, the
resolved history and the priority/blocking axes no longer exist) and the Inbox and counter parts of
[restore the unread dot](2026-11-10-restore-unread-dot.md) (point 6 no longer has a counter to hide).
