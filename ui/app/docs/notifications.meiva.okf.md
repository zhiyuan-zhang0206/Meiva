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

The bridge keeps the one job the server cannot do: deduplicating by
`notification_id` inside the webview session and turning a native notification
tap into the Inbox deep link (`app_take_pending_click`).

Android posts through the notification plugin's mobile path off the main looper
(`android::show_notification`); macOS and Linux post through the same plugin from
`desktop::show_notification`, where the permission prompt follows the switch in
settings rather than the first event. Neither platform decides *what* is worth
notifying — `app_notify` rechecks the persisted consent and the bridge carries no
trigger of its own.
