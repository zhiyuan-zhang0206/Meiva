# App notification delivery

One bridge serves both platforms: `scripts/notify-bridge.js` is injected into the console webview,
subscribes to the gateway's `/api/system` broadcast and renders only the `notification` role,
whose delivery the gateway's spine already decided (the two triggers, the category switches and
the delivery log are server-side). See
[the notification spine](../../../base/events/notifications/README.md).

Each event carries an `action` (`alert`, `replace`, `retract`) and a stable `tag`
(`unread:<agent id>`, `question:<notice id>`). The bridge keeps one notification per tag.

## What the bridge decides

- **Not for the agent being read.** The server cannot know who is in the foreground, so the bridge
  drops an unread notification when the window is visible and focused and the console URL has that
  agent open (`?agent_id=`, the state the sidebar's reading marker follows; the webview is the
  console page itself, so this needs no extra IPC). A question is never dropped. If the cursor
  advance the open console sends arrives later, its `retract` still takes the notification down.
- **Alert versus silent replace.** An `alert` raises the notification. A `replace` updates the
  one this session already raised without a sound; for a tag never raised in this session it
  alerts once, because the user has never been told about it.
- **Folding.** Live events are handled one at a time; a catch-up gap is folded to the final event
  of each tag, so alert, replace, replace shows one alert with the newest content, and a tag whose
  final event is a retract shows nothing (the retract is still sent, to take down a notification
  raised before the webview restarted). Only tags that still exist alert, and only once.
- **Deduplication.** The session remembers the last 200 `notification_id`s, which bounds the
  duplicate window of the at-least-once announce rather than removing it.

## Catch-up

The live event is a Redis publication, so a webview that was asleep or offline missed it. The
bridge keeps a delivery cursor in `localStorage` (`meiva.notify.cursor`) and on each (re)connect
asks `GET /api/notifications?after_id=<cursor>&limit=100`. The cursor advances only over rows the
bridge actually read: a live event renders at once but only schedules a catch-up, because an event
proves its own delivery exists, not that nothing before it was missed. A first run has no cursor
and adopts `latest_id` instead of replaying the log. A gap wider than 100 rows keeps the newest
100.

## Tap target

The target is the delivery's own `href` (`/?agent_id=<id>`), persisted under `meiva.notify.target`
when rendered, because a tap is consumed by the native side, which then asks the bridge where to
go. Only a same-origin path is accepted: a scheme, a protocol-relative `//host` or a backslash is
refused, since the value comes off the wire. The console root is the fallback.

## Platform capability

`app_notify(mode, tag, title, body)` is the one native command (`src/commands.rs`); it rechecks
the persisted consent first.

| | alert | replace | retract |
|---|---|---|---|
| Android | `builder().id(h(tag))`, default channel (sounds) | only if that id is still shown (`active()`): `id(h(tag))` on the low-importance `meiva_updates` channel, which the plugin posts after cancelling the old one, so it is silent | `remove_active([h(tag)])` |
| macOS / Linux / Windows | `builder().show()` | not possible: the plugin's desktop API has no id, no silent update and no withdraw | not possible |

`h(tag)` is a stable non-negative FNV-1a hash (`src/notify.rs`). The Android plugin cancels a
visible notification of the same id before re-posting it (`TauriNotificationManager.trigger`),
which defeats `setOnlyAlertOnce`, so silence comes from the quiet channel, not the id. The desktop
plugin (`notify-rust`) exposes only title, body, icon and sound, so a desktop shows the first
alert of an unread round and leaves it in the notification centre; a replace would pop a second
banner, so it is skipped.

Android posts through the notification plugin's mobile path off the main looper
(`android::show_notification`) with **zero persistent foreground-service notification**:
notifications appear only when a real notification event is delivered over active or recent
background SSE or caught up from `GET /api/notifications?after_id=<cursor>` when the app resumes
(`visibilitychange` / `EventSource.onopen`). Neither platform decides *what* is worth
notifying: the bridge carries no trigger of its own.

## Verification

`ui/app/tests/test_notify_bridge.py` runs the checked-in bridge against a fake gateway under node
(alert, silent replace, retract, foreground suppression, catch-up folding, deduplication). The
Rust crate is checked by CI (`cargo check` and clippy on the desktop hosts, the Android APK build);
the Android code path and the real notification surfaces can only be confirmed on a device:

1. Android: with agent A not open, have it deliver output: one sounding notification titled with
   A's name. Have it deliver again: the same notification updates with no sound. Open A: the
   notification disappears. Have A deliver once more: it sounds again.
2. Ask a question from an agent: its own sounding notification. Answer or close it in the console:
   that notification disappears. Two questions are two notifications.
3. With A open in the focused window, have it deliver output: nothing is shown. Switch the app to
   the background or open another agent and deliver again: it alerts.
4. Background the app, produce several deliveries and a question, reopen it: one notification per
   agent, with the newest content, and the answered question absent.
5. macOS: step 1 shows one banner per unread round; later output adds no banner.
