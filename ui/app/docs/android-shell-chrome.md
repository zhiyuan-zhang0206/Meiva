# Android shell chrome

## System bars

Tauri's generated `MainActivity` calls `enableEdgeToEdge()`, so the webview's
top edge is y=0 under the status bar and its bottom edge under the navigation
bar. The build-time overlay pads `android.R.id.content` by the system bars and
display cutout so both the hosted console and bundled pages sit inside the usable
area, including on older WebViews.

[Android WebView](https://developer.android.com/develop/ui/views/layout/webapps/understand-window-insets)
maps delivered system bar and cutout insets to CSS `env(safe-area-inset-*)` on
all WebViews starting with Chromium M144. After padding the content root,
`MainActivity` returns `insets.inset(bars.left, bars.top, bars.right, bars.bottom)`.
This subtracts the padding from the insets dispatched to the WebView, so the
page avoids applying the same safe area twice. Inset updates still propagate,
including the remaining IME (soft keyboard) inset for viewport resizing.

The overlay requests inset dispatch after installing the listener. The patch
is idempotent and rejects unrecognised activity templates
(`tests/ui/test_android_overlay.py`).

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
