---
type: doc
title: Meiva App
description: Thin Tauri 2 native host shell that loads the remote Meiva console on macOS and Android, adding native login, event-driven notifications, and updates across the window.__MEIVA_APP__ bridge.
tags:
  - frontend
  - desktop
  - android
  - tauri
---

# Meiva App

## What it is

`ui/app/` is one Tauri 2 native host shell for macOS and Android. It bundles
only the small onboarding/retry/settings surface in `app-ui/`; the product UI
is always the remote gate-served [[../../web/docs/web.meiva.okf.md|Next.js console]].
Across the three-layer UI architecture (**Core HTTP/SSE contracts** ↔ **Shared
Web Console (`ui/web`)** ↔ **Native Host Shell (`ui/app`)**), the shell owns
the native window and platform capabilities that a browser tab cannot supply,
and communicates with the web console through the single `window.__MEIVA_APP__`
bridge contract (`ui/web/src/components/app/bridge.ts`).

## Entry and trust boundary

Address normalization, the 30-second connecting probe, Keystore/env auto-login,
and the scoped remote IPC capability live in their own node:
[[ui/app/docs/entry-and-trust.meiva.okf.md|App entry and trust boundary]].

## Desktop

`src-tauri/src/desktop.rs` provides one-instance behavior, tray open/quit,
close-to-tray, launch-at-login, and Tauri updater checks. The app ships without
branding icons: the bundle declares none, the tray item renders without an
image, and `icons/` keeps only the blank `.png` placeholder the
toolchain requires. Auto-login reads the
local `$MEIVA_HOME/.env` secret, performs the gateway login outside web content,
and reloads after its native cookie store receives the session.

## Android

Android provides Keystore-backed auto-login and event-driven local
notifications — notifications appear only when an actual agent notification
event arrives (or is caught up on resume/reconnect), never as a permanent
foreground-service notification. A generated Tauri Gradle project is patched at
build time by `android/apply_overlay.py`; the checked-in Kotlin plugins provide
the Android-Keystore secret bridge, CookieManager session-cookie injection,
notification-click capture, and APK update verification.
`MeivaClickPlugin` captures a notification tap so the bridge can open the
delivery's notice route (or `/fleet#inbox` fallback): after its SSE opens, the
bridge consumes the per-tap flag once and navigates the same window.
The overlay also keeps the JNI-named
Tauri plugin classes and reflectively discovered commands through release
minification. The injected SSE bridge listens to `/api/system` on the remote
console origin and renders the delivered `notification` events; it decides
nothing itself (see below). Notification IPC also rechecks persisted consent
natively. The bridge starts from a fresh prelude or waits once for its
`meiva-app-config` event. All Android plugin calls leave the main looper before waiting for
their JNI response; direct synchronous mobile-plugin calls would deadlock it.

System bars and how the shell integrates with the console through the host
bridge are their own node:
[[ui/app/docs/android-shell-chrome.meiva.okf.md|Android shell chrome]].

## Android updates

The manifest-driven update path — build-number ordering, digest and signer
verification, the installer hand-off, and both entry points — has its own node:
[[ui/app/docs/android-updates.meiva.okf.md|Android updates]].

## Build and release

`Cargo.toml` is version `1.0.0` and mirrors the product version in
`pyproject.toml` (the release-surface guard `tests/ci/test_version_pins.py`
keeps the two equal); release tags (`app-v*`, `android-v*`) must match it
exactly — the release workflows fail on mismatch — so a release starts by
bumping `pyproject.toml` and its `Cargo.toml` mirror.
`.github/workflows/ci-app.yml` path-filters Rust format/clippy, Android-target
checking, and overlay/manifest tests.
`release-app.yml` builds the universal macOS DMG; `release-android.yml` builds the Android APK (`android-v*` tags)
artifacts for `app-v*` tags. The client updates from the `android-latest.json`
manifest published beside the rolling `android-latest` APK, not from tags. Apple signing and updater signing activate only for
complete secret groups; tag releases fail closed when any required signing
group is absent, while manual dispatch may still produce unsigned evidence.
`scripts/codegen/build_app_update_manifest.py` emits signed updater entries only, and
the mutable `app-latest` release carries the stable `latest.json` endpoint.

`release-android.yml` injects `versionCode = 1_000_000 + github.run_number`
through a Tauri `--config` overlay, reads it back from the APK with `aapt2`, and
refuses a publish that would not advance the advertised code.

Notification delivery on both platforms is its own node:
[[ui/app/docs/notifications.meiva.okf.md|notifications]].

## Limits

Android notification SSE and catch-up run in the webview without a persistent
foreground service; notifications while the app is suspended are caught up via
`GET /api/notifications?after_id=<cursor>` when the app resumes, while instant
closed-app push delivery would require an external push service and is out of
scope. An update always ends at the system installer's
confirmation: the app can only prepare and verify the file. Android cleartext CIDR enforcement is an
application-time DNS check because the platform XML supports domain names, not
network prefixes.

## Key paths

- `src-tauri/src/` — Rust lifecycle, settings, ACL, navigation, platform wiring
- `src-tauri/scripts/` — scripts injected into the remote console
- `app-ui/index.html` — bundled onboarding/retry/settings UI
- `android/` — Kotlin/XML/ProGuard Keystore, cookie, click, and update overlay plus deterministic patcher
- [README](../README.md) — local build and signing commands
- [Future distribution work](../../../future/ui-app.md) — remaining external work
