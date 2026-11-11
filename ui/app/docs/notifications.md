# App notification delivery

One bridge serves both platforms: `scripts/notify-bridge.js` is injected into the console webview,
subscribes to the gateway's `/api/system` broadcast and renders only the `notification` role,
whose delivery the gateway's spine already decided (the two triggers, the category switches and
the delivery log are server-side). See
[the notification spine](../../../base/events/notifications/README.md).

Each event carries an `action` (`alert`, `replace`, `retract`) and a stable `tag`
(`unread:<agent id>`, `question:agent:<agent id>`). The bridge keeps one notification per tag.

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
| macOS, from the app bundle | request `meiva.notify.<tag>` with sound | only if that identifier is still delivered (`getDeliveredNotifications`): same identifier, passive interruption level, no sound, so the content changes in the notification centre without a banner | `removeDeliveredNotifications` |
| macOS bare binary (`tauri dev`), Linux, Windows | `builder().show()` through the plugin | not possible: the plugin's desktop API has no id, no silent update and no withdraw | not possible |

`h(tag)` is a stable non-negative FNV-1a hash (`src/notify.rs`). The Android plugin cancels a
visible notification of the same id before re-posting it (`TauriNotificationManager.trigger`),
which defeats `setOnlyAlertOnce`, so silence comes from the quiet channel, not the id. The desktop
plugin (`notify-rust`) exposes only title, body, icon and sound, so Linux and Windows show the
first alert of an unread round and leave it in the notification centre; a replace would pop a
second banner, so it is skipped.

### macOS native path

On macOS the three modes do not touch the plugin at all (no second, duplicate notification):
`src/macos_notify.rs` talks to `UNUserNotificationCenter` through the `objc2-user-notifications`
bindings, and `notify::decide_mac` (pure, unit tested) decides the operation.

- **Identifier.** `meiva.notify.<tag>`, a pure function of the tag, so a replace or retract finds
  the notification an earlier alert posted, across webview reloads and app restarts.
- **Dismissed stays dismissed.** A replace first asks the centre what is still delivered and does
  nothing when the identifier is gone, as on Android: a notification the user swiped away is not
  pulled back by later output. An alert is the deliberate "tell the user again" and always posts.
- **Tap.** A delegate (`UNUserNotificationCenterDelegate`) shows the window and sets the pending
  click flag that `app_take_pending_click` consumes, so the bridge jumps to the newest delivery's
  target exactly as on Android. The delegate also lets a notification present while the app is in
  the foreground (banner and sound for an alert, list only for a quiet update).
- **Bundle required.** The notification centre exists only for a process inside a `.app` with a
  bundle identifier (`com.meiva.app`); asking for it from a bare binary raises an Objective-C
  exception. `macos_notify::is_available` checks the main bundle, and `tauri dev` falls back to
  the plugin path above (alert only, posted under Terminal's identity by the plugin).
- **Authorization.** `requestAuthorization(alert, sound)` runs when the notifications switch is
  turned on and at startup when it is already on. The system prompt appears once per bundle
  identifier and the answer is remembered; a denial is not re-prompted. The plugin on macOS never
  requested a permission (its `request_permission` returns granted without asking), so an install
  that notified through the plugin before this path may see the prompt for the first time.
- **Verified so far.** A locally built, ad-hoc signed debug bundle reaches the centre (bundle
  identifier present, no exception) and reports authorization `notDetermined` until the user
  answers the system prompt; nothing is delivered before that. Banner, silent replace and
  retract are therefore confirmed by the manual steps below, not by CI.
- **Signing.** A release build is Developer ID signed and notarized (`release-app.yml`); a build
  without the certificate is ad-hoc signed. Both are bundles with an identifier, so the centre
  works for both; the identity only matters to Gatekeeper on first open. Moving an app between
  signing identities can make macOS treat it as a different app for the permission.

Android posts through the notification plugin's mobile path off the main looper
(`android::show_notification`) with **zero persistent foreground-service notification**:
notifications appear only when a real notification event is delivered over active or recent
background SSE or caught up from `GET /api/notifications?after_id=<cursor>` when the app resumes
(`visibilitychange` / `EventSource.onopen`). Neither platform decides *what* is worth
notifying: the bridge carries no trigger of its own.

## Verification

`base/events/notifications/tests/test_notify_bridge.py` runs the checked-in bridge against a fake gateway under node
(alert, silent replace, retract, foreground suppression, catch-up folding, deduplication). The
Rust crate is checked by CI (`cargo check` and clippy on the desktop hosts, the Android APK build)
and `cargo test` covers the pure decisions (`notify::decide_mac`: identifier, the three modes,
dismissed-stays-dismissed). The real notification surfaces can only be confirmed on a device:

1. Android: with agent A not open, have it deliver output: one sounding notification titled with
   A's name. Have it deliver again: the same notification updates with no sound. Open A: the
   notification disappears. Have A deliver once more: it sounds again.
2. Ask questions from an agent: one sounding notification for the agent (a second ask only updates
   it). Answer or skip all of them in the console: that notification disappears.
3. With A open in the focused window, have it deliver output: nothing is shown. Switch the app to
   the background or open another agent and deliver again: it alerts.
4. Background the app, produce several deliveries and a question, reopen it: one notification per
   agent, with the newest content, and the answered question absent.
5. macOS (installed or locally built `.app`, notifications switch on; allow the system prompt the
   first time): step 1 shows one banner and sound per unread round; later output changes the
   content of that same entry in the notification centre with no banner or sound; opening the agent
   removes it. Swipe the notification away, deliver again: it does not come back. Click it: the
   window opens on the agent. Run from `tauri dev` instead and only the first alert appears.
