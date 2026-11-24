# The two system notifications are always on

Date: 2026-11-24

## Context

[2026-11-11](2026-11-11-notifications-are-questions-and-unread.md) left the product with exactly
two pushes, an agent asked a question and an agent has unread output, and kept a per-category
switch for each in Display settings → Notifications (`notifications.categories` in
`user_settings`). Both switches shipped on, and a push that is off is a question the user never
sees or output they never learn about.

## Decision

Neither push can be switched off. The settings rows, `GET /api/notifications/categories`, the
`notifications.policy` module and the category label, description and default-on fields are gone;
every plan the producers make is staged. The retired `notifications.categories` key is deleted from
`user_settings` by migration and nothing reads it: a stored `false` from before this decision has
no effect, so no one stays locked into a switched-off state.

The app's own consent (the operating-system permission prompt and the app's notifications opt-in,
`ui/app/docs/notifications.md`) is a platform boundary, not a category switch, and stays.

## Consequences

Suppressing a push is a code change with its own decision, not a user preference. A client may
still render less than the spine delivered (the bridge drops an unread push for the agent open in
the focused window), never more.
