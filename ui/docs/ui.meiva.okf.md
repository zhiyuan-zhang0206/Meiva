---
type: doc
title: User interfaces
description: Shared web console and cross-platform native host shell (macOS and Android) over gateway HTTP/SSE contracts.
tags: []
---

# User interfaces

The UI domain is organized around a strict three-layer boundary:

1. **Core HTTP/SSE contracts** ([[gateway/docs/gateway.meiva.okf.md]]) — authentication, agent lifecycle, timeline, fleet supervision, and delivered notification events.
2. **Shared web console** ([[ui/web/docs/web.meiva.okf.md]]) — the single Next.js product UI served behind the gate for both browser tabs and native shells.
3. **Native host shell** ([[ui/app/docs/app.meiva.okf.md]]) — one thin Tauri 2 application for **macOS and Android** that loads the remote web console and adds only platform-native capabilities (window/tray residency, Keystore/env auto-login, event-driven local notifications, and native updates) across the typed `window.__MEIVA_APP__` host bridge (`ui/web/src/components/app/bridge.ts`).

Their component nodes own implementation details; this overview defines the layer boundary without duplicating them.
