---
type: doc
title: App notification delivery
description: How the app renders the gateway's delivered notifications on Android and macOS.
tags: [contracts]
---

# App notification delivery

One bridge serves both platforms: `scripts/notify-bridge.js` subscribes to the
gateway's `/api/system` broadcast and renders only the `notification` role,
whose delivery the gateway's spine already decided — category switches,
deduplication and the delivery log are server-side. See
[[base/events/notifications/docs/notifications.meiva.okf.md|the notification spine]].

The bridge keeps the two jobs the server cannot do: deduplicating by
`notification_id` inside the webview session and turning a native notification
tap into the Inbox deep link (`app_take_pending_click`).

Delivery is at-least-once by design: a crash between the publish and the mark
re-announces the same row, so the same `notification_id` can arrive twice. The
session remembers the last 200 ids, which bounds the duplicate window rather than
removing it — a repeat that lands more than 200 deliveries later notifies again.
Any consumer that replaces this bridge has to deduplicate on `notification_id`
itself; the server will not do it for them.

The live event is a Redis publication, so a webview that was asleep or offline
missed it outright. The bridge therefore keeps a delivery cursor in
`localStorage` (`meiva.notify.cursor`) and on each (re)connect asks
`GET /api/notifications?after_id=<cursor>&limit=10` for what it missed — the
newest deliveries after the cursor, oldest-first, next to the newest id in the
table. The cursor advances only over rows the bridge actually read: a live event
renders at once but only asks for a catch-up, because an event proves that its
own delivery exists, not that nothing before it was missed — events can arrive
out of order, and an earlier delivery can have been published while the webview
was away. Advancing to `latest_id` on every read instead would jump the cursor
over rows that were committed between the read and the next look. A first run
has no cursor and adopts `latest_id` instead of replaying the delivery log at
the user; a gap wider than the ten-row page — a long offline stretch, or more
than ten deliveries whose live events were dropped while the session was
connected — is served the newest ten and skips the rest, which is the right
trade for a ping: the console keeps the full log.

Android posts through the notification plugin's mobile path off the main looper
(`android::show_notification`); macOS and Linux post through the same plugin from
`desktop::show_notification`, where the permission prompt follows the switch in
settings rather than the first event. Neither platform decides *what* is worth
notifying — `app_notify` rechecks the persisted consent and the bridge carries no
trigger of its own.
