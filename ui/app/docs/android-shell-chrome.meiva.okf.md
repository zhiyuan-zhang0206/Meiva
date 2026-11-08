---
type: doc
title: Android shell chrome
description: How the Android shell keeps the webview out of the system bars and integrates with the console through the host bridge.
tags:
  - android
  - tauri
---

# Android shell chrome

## System bars

Tauri's generated `MainActivity` calls `enableEdgeToEdge()`, so the webview's
top edge is y=0 under the status bar and its bottom edge under the navigation
bar. A plain Android WebView reports no CSS safe-area insets to the page, so
the console cannot pad itself out of the system UI: the fix has to be native.
The build-time overlay pads the generated activity's content root by the system
bars plus the display cutout and re-requests the insets after installing the
listener, so every page sits inside the usable area on edge-to-edge devices and
the padding is zero where the window already fits. The patch is idempotent and
refuses an unrecognised activity template rather than shipping an APK whose
header hides under the status bar (`tests/ui/test_android_overlay.py`).

## Host bridge and console chrome

The console page carries no app-injected floating DOM controls. Both the route
back to the bundled settings page and the Android in-app update banner flow
through the typed host shell bridge (`window.__MEIVA_APP__`, read by
`ui/web/src/components/app/bridge.ts`):

- The Control page's App section renders only when `readAppBridge()` resolves a
  host bridge (`window.__MEIVA_APP__.invoke`) and calls `app_open_settings`,
  which the console origin is granted at runtime.
- `update-check.js` publishes available update state onto
  `window.__MEIVA_APP__.update` and dispatches `meiva-app-update` without
  mutating the DOM; `AppUpdateBanner` (`ui/web/src/components/app/app-settings.tsx`)
  renders the prompt in the normal React flex layout flow at the top of the shell
  so it never overlaps the header or composer band.
- The failure screen still offers "Change address".

Android's network-security XML cannot express IP prefixes, so it permits
cleartext transport while Rust refuses to persist an HTTP endpoint unless all
resolved addresses are loopback, link-local, RFC1918, or `100.64.0.0/10`.
HTTPS is unrestricted.
