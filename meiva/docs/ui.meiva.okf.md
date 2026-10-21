---
type: doc
title: meiva.ui — User Interface
description: '`meiva.ui` lets agents display rich web pages and post notices to users; serve() pages run in persistent shells owned by their agents.'
tags:
- agent-view
- sdk
- agent-lifecycle
---

# meiva.ui — User Interface

## What it is

`meiva.ui` allows agents to display rich web pages and post notices to users. `serve()` runs in a persistent shell session owned by the serving agent and managed by the page-server daemon; `show()` registers an HTTP server the agent already owns. The page server binds the machine's reachable host (loopback on a single box) and is served to the user through the **gateway reverse proxy** — the link is the gateway's own authenticated URL (`/pages/<id>-<name>/`), so the browser never dials the page server directly.

## Core API (core SDK: page serving)

Only one active page is allowed at a time (opening a new one auto-closes the old). Ports are explicit: `serve`/`show` require `port` (1024-65535) — there is no per-agent reserved or default port — and the platform refuses a port another live page already holds. The conflict check runs before the auto-close, so a refused registration leaves the agent's current page untouched. `serve` additionally fails before anything is registered when the port is held by a process that is not a page server (the daemon never displaces a foreign occupant).

- `serve(dir, name, port, title=None) → Page` — One-step: start directory HTTP server + register with UI, poll `/health` until listening. A relative `dir` resolves against the agent's working directory (`meiva.cwd`), consistent with the `meiva.files` API.
- `show(name, port, title=None) → Page` — Register an already running HTTP server (does not probe whether anything is listening; the registry still refuses a port another live page holds).
- `close(name)` — Deregister page and kill the server started by `serve()`.

Page registration and close are keyed HTTP operations: each call sends one
`Idempotency-Key` with the `principal-v1` scope and retries an ambiguous
response under it, and the gateway replays the original acceptance instead of
replacing the current page or closing a later one. `close` reads the open pages
first and closes exactly the observed registry row. A returned registry row is
acceptance, not proof the name-based URL still serves that page. Before an
explicit new show/serve/close intent, inspect current pages; no client outbox is
provided by these calls.

`name` must match `^[a-zA-Z0-9_-]+$` (1-64 chars). Returned `Page`: id, name, port, title, url.

### Server lifecycle
The daemon creates one persistent shell for each fresh `serve()` registration and persists its session name before launch. It adopts recorded live sessions after its own restart. A missing session, missing directory or unhealthy server closes the page and logs a warning; it does not relaunch or message the agent. Explicit registration can serve again. Normal close, TTL and identity-checked cleanup remain in place. See [[agent/startup/docs/page-restore.meiva.okf.md]].

## meiva_fleet Plugin Injections (Notifications)

The following are mounted onto the `meiva.ui` namespace by the `meiva_fleet` plugin (non-core). See plugin-side OKF for details.

- `notify(title, content=None, *, require_response=True, blocking=False, priority='P2', task=None, expire_at=None, idempotency_key, question=None) → Notice` — Post a notice to the user's inbox. One open notice per agent: while one is open, a second call raises with that notice's id and the two ways out (dismiss it, or edit it in place); nothing is replaced silently. Every notice requires a reply; `require_response=False` is rejected; `blocking=True` indicates you are blocked waiting. The returned `Notice` is an int subclass with `.pending_count` / `.pending_notices`. A notice whose deadline passes closes as `expired`: no reply arrives, the slot frees, and a system message says so.
- `edit_notice(*, title=..., content=..., priority=..., blocking=...)` — Edit the open notice (only pass fields to change; cannot change `require_response`; at most one is open, so no id).
- `dismiss_notice()` — Dismiss the open notice (at most one is open, so no id).

### Priority
P0 (highest) → P1 → P2 (default) → P3 (lowest). The notice queue orders by priority, then blocking and age. Priority does not change agent execution order.

## Key Dependencies
- [[ui/web/src/docs/frontend.meiva.okf.md]] — Frontend UI renders pages and notifications

## Notes
`notify` requests a user answer through an asynchronous queue. Ordinary information,
progress and results belong in chat. Priority and blocking are independent; every
notice requires a response even when the agent can continue other work. Peer
decisions go to that peer through `send_message`.
