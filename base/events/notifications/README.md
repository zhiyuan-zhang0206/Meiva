# Notification spine

A system notification has exactly two triggers: an agent **asked a question**, and an agent has
**unread output**. Nothing else is one; any other event belongs in the log, the task board or the
timeline that already carries it. The decision is recorded in
[system notifications are questions and unread output](../../../docs/decisions/engineering/notifications/2026-11-11-notifications-are-questions-and-unread.md).

`base/events/notifications` owns the delivery: a producer plans a push (`triggers.py`, pure), the
spine applies the user's per-category switch, records the delivery in the same transaction as the
write that caused it, and publishes one live event every transport consumes.

## Pushes

A push is a delivery row with a category, an `action` and a stable `tag`. A client keeps at most
one notification per tag, so the tag is the whole replace/retract contract.

| Trigger | Category | Tag | Alert | Silent replace | Retract |
|---|---|---|---|---|---|
| A question is asked | `core.question` | `question:<notice id>` | when asked | never | when answered or closed |
| An agent delivers output the user has not read | `core.unread` | `unread:<agent id>` | first delivery after everything was read | each later delivery (newest content) | the seen cursor reaches the newest delivery |

Unread is the seen cursor, not stored state. `publish()` in
`base/agents/to_user/delivery/store.py` reads `has_unseen` before inserting: no unread means
`alert`, an unread delivery already outstanding means `replace`. `advance_seen` reads it before and
after the cursor moves and stages the `retract` only when the move cleared the last unread
delivery. A replayed publication key and a "no update" (`content` null) stage nothing. The three
paths that deliver to the user (the native turn hook, the external harness callback, the SDK
`POST /api/agents/{id}/user-messages`) all go through `publish()`, so none stages on its own.
The Ask insert (`gateway/agents/user_delivery/asks.py`) stages the question alert and the notice
resolution (`gateway/agents/notice_operations/notices.py`) stages its retract.

The title of an unread push is the agent's label (`#<id>` when it has none) and its body is the
opening of the newest delivery with the visibility prefix and Markdown markers removed
(`preview.py`); a question's title is the question title and its body the agent's label. Text is not
translated. `href` is the agent's page (`/?agent_id=<id>`).

## Store

`notification_events` is a delivery log: one row per push, never answered or resolved. Columns of
note: `category`, `action`, `tag`, `subject`, `body`, `href`, `importance`, `agent_id`,
`announced_at` (`dedupe_key` is retired and unused). The hosted runtime writes the same table, so
the runner role has `SELECT, INSERT` on it plus `UPDATE (announced_at)`, and only `SELECT` on
`agents`, `user_settings` and `agent_user_seen`, which is all staging reads.

The table is also the outbox. `announced_at` is when the live event was published; NULL means the
push is still owed. Producers only stage the row inside their transaction; they never announce.
The gateway sweep (`gateway/events/notification_outbox.py`, every
`notification_outbox_interval_seconds`, two seconds by default, bounded by
`notification_outbox_batch`) publishes every committed unannounced row and marks it only after
the transport accepted it. A known Redis failure returns None and marks nothing, so the next
sweep retries it. A publish with no subscriber is accepted too: Redis took it and nobody was
listening, so the row is marked announced and an offline client gets it from the recovery read.
Re-announcing after a lost mark is at-least-once on purpose: clients deduplicate on
`notification_id`.

## Categories and policy

`catalog.CORE_CATEGORIES` holds the two categories, both on by default. The only suppression is the
user's switch (`notifications.categories`, one boolean per category id; Display settings →
Notifications). It gates an alert or a replace; a retract is always staged, because switching a
category off must not strand a notification that is already on a screen.
`GET /api/notifications/categories` returns the categories with the switches applied.

## Transports

`ui/app/src-tauri/scripts/notify-bridge.js` renders the live event on Android and macOS and
catches up after a gap through `GET /api/notifications?after_id=`: the live event is a Redis
publication with no memory, so the delivery log is the only thing a client that was offline can
ask. The endpoint returns the newest deliveries after the cursor (oldest first, at most 100) plus
the newest id, so a first-run client adopts a cursor instead of replaying history. The client folds
a gap to the final state of each tag, so only what still exists is shown, and alerts at most once
per tag. Whether the user is looking at an agent is a client fact (the server cannot know it): the
bridge does not show an unread notification for the agent open in the visible, focused window.
The console renders the switches and no delivery feed of its own, so the `notification` role never
enters a timeline. No transport re-derives its own trigger from agent state. A transport may
render less (an importance floor, a master switch), never more. See
[app notification delivery](../../../ui/app/docs/notifications.md) for what each platform can do.
