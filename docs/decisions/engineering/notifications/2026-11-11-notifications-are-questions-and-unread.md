# System notifications are questions and unread output

Date: 2026-11-11

## Context

The notice mechanism is gone: an agent can no longer post a notice, and the last non-question
writer (the task escalation reminder) was removed, so the notification spine of
[2026-10-18](2026-10-18-one-notification-spine.md) had no producer left. The user-facing model is
now three things: output delivered to the user (`agent_user_messages`), questions
(`meiva.ui.ask`, `agent_notices` rows with a question), and one "seen" cursor per agent
(`agent_user_seen`). A system notification should follow that model and nothing else.

## Decision

A system notification has exactly two triggers.

- **An agent asked a question.** One notification per question, each alerting on its own, tagged
  `question:<notice id>`. Answering or closing the question retracts it.
- **An agent has unread output.** One notification per agent, tagged `unread:<agent id>`, whose
  content is always the newest delivery to the user (the agent's display name as the title, the
  opening of the output as the body). The first delivery after everything was read alerts once;
  every later delivery silently replaces the content of that one notification; when the seen cursor
  reaches the newest delivery the notification is retracted, and the next delivery starts a new
  round and alerts again. Nothing is counted.

Unread is derived from the cursor, never stored: `has_unseen` before the write decides alert versus
replace, and `has_unseen` before and after a cursor move decides the retract. Staging happens in
the transaction of the write that causes it (`publish()` in the delivery store for all three
delivery paths, the Ask insert, the question resolution, the cursor advance), so a rolled-back write
leaves no notification. A delivery row carries `action` (`alert`, `replace`, `retract`) and `tag`.

Whether the user is looking at the agent is a client fact: the server cannot know it. The app
bridge drops the unread notification for the agent that is open in the visible, focused window.
Questions are not subject to that rule.

A client keeps one notification per tag, folds a catch-up gap to the final state of each tag, and
alerts at most once per tag for what it finds there.

Removed with this decision: the `core.needs_input` category and `stage_notice_notification`, the
dedupe window, plugin-declared categories (`contributions.notifications`,
`POST /api/agents/{id}/notifications`), `meiva notices audit` and `GET /api/notifications/audit`.
Third-party plugins that declared notification categories lose that surface; the built-in plugins
never used it. The settings page has two switches, both on by default.

## Consequences

The push channel cannot grow a trigger without a decision here. Desktop platforms whose
notification API cannot replace or withdraw a shown notification (Linux, Windows) show the alert
only (see `ui/app/docs/notifications.md`). macOS does not: the app bundle posts through
`UNUserNotificationCenter` under an identifier derived from the tag, so `replace` updates the shown
entry quietly (passive interruption level), `retract` removes it, and a notification the user
dismissed is not brought back. The notification plugin remains the path only where there is no
bundle (`tauri dev`) or no native API. The outbox sweep is now the only announce path, so its default
interval is two seconds, which bounds how late a push reaches a connected client. `dedupe_key` and
its index stay in `notification_events` until a later contract migration.
