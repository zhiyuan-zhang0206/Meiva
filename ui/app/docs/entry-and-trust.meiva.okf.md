---
type: doc
title: App entry and trust boundary
description: How the native host shell resolves gate/gateway addresses, probes reachability, exchanges session credentials, and scopes remote IPC permissions.
tags:
  - frontend
  - desktop
  - android
  - tauri
---

# App entry and trust boundary

`settings.json` in the platform app-config directory is the persisted
non-credential configuration. `entryUrl` names the gate; `gatewayUrl` is
optional and otherwise derives from the same host on port 8000. Desktop defaults
to `localhost:3000`; Android starts on onboarding. Android's primary server
field normalizes a bare host or a default gateway URL into the console origin
on 3000, preserves `https`, and removes paths, queries, and fragments. Other
Android primary-field ports are refused with an advanced-options hint; the
optional `gatewayUrl` override keeps its existing custom-port capability (and
maps a bare host or `:3000` to `:8000`). Desktop shares the one-field page but
preserves its existing arbitrary entry and gateway URLs for worktree clusters.

The bundled onboarding page (in the device locale from the webview language) immediately changes to a 30-second
connecting state while saving. `app_save_settings` persists Android-normalized
settings, then defers the window change until the asynchronous IPC reply flushes.
Android refreshes the existing webview's prelude and navigates it rather than
destroying it; its navigation allowlist and page-load prelude resolve from live
settings so the new console origin and notification bridge see the saved
configuration. Desktop keeps its rebuild. A hidden Android page retains its
timeout recovery until it becomes visible. `window.rs` probes the console root
with a four-second HTTP GET rather than TCP, treats
2xx/3xx/401/403 as healthy, and sends a final `reason=unreachable|http|update-window`
query into the bundled failure screen within the retry budget.

`src-tauri/src/window.rs` builds one webview and makes bounded HTTP probes of
the gate while it connects. Native navigation accepts only the exact gate
and gateway origins plus bundled Tauri assets. New-window links leave through
`src-tauri/src/external.rs`: local headed cluster Chrome first on supported Unix
desktops, then the system browser. Only HTTP(S), `mailto:`, and `tel:` may cross
that OS-opening boundary.

The bundled origin gets settings commands through `capabilities/local.json`.
The configured remote console gets a narrower runtime capability with no
credential-reading command. Desktop auto-login exchanges the local cluster
secret from `$MEIVA_HOME/.env` in native Rust. Android accepts an optional secret
only in the bundled form; after the first successful native login it encrypts
the value with Android Keystore and stores ciphertext in SharedPreferences. In
both cases the webview receives only the resulting HTTP-only session cookie;
401/403 falls back to the console login without retrying or clearing Android's
stored credential. Android loads that credential through an asynchronous native
plugin call into a process-local cache; a first window prelude may report
`autoLogin: false` while loading, but startup login uses the same loader and
does not block the Android main looper.

Parent node: [[app.meiva.okf.md|Meiva App]].
