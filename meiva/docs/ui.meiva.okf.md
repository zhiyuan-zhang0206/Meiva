---
type: doc
title: meiva.ui — User Interface
description: '`meiva.ui` lets agents display rich web pages and publish results and ask users questions; serve() pages run in persistent shells owned by their agents.'
tags:
- agent-view
- sdk
- agent-lifecycle
---

# meiva.ui — User Interface

## What it is

`meiva.ui` allows agents to display rich web pages and publish results and ask users questions. `serve()` runs in a persistent shell session owned by the serving agent and managed by the page-server daemon; `show()` registers an HTTP server the agent already owns. The page server binds the machine's reachable host (loopback on a single box) and is served to the user through the **gateway reverse proxy** — the link is the gateway's own authenticated URL (`/pages/<id>-<name>/`), so the browser never dials the page server directly.

## User delivery in SDK mode

`user_delivery_mode=sdk` routes formal communication through core SDK calls:

- `publish(content, *, suggestions=None) -> int` appends a durable
  publication. Optional suggestions supply candidate replies to the independent
  composer service. Omit them when no reply is useful.
- `ask(title, content=None, *, question=None, note_suggestion=None) -> int` appends a question and returns its global ID. Omitted
  question means free text; `single_select` and `multi_select` accept declared
  options. Every type has an editable Note, optionally seeded by the agent.
- `publications(pending_only=True, after_id=0, limit=50)` reads durable messages
  in increasing ID order without marking them read; continue after the last ID.
- `questions()` reads up to 500 open questions without answering them.

The runtime owns operation identity for SDK writes and deterministic replay.
The gateway refuses changed content under an accepted key. Several questions
may remain open concurrently. New Ask
records do not expire automatically. Their content and answers remain durable.

Publishing or asking during work never ends the turn. Ordinary assistant text,
including final summaries, stays in expandable Details. At natural completion,
the runtime checks that the turn published or asked at least once; it gives one
corrective retry if neither happened. `publish(None)`
explicitly records that no user update is needed.

Publications carry unread state; Ask carries awaiting-answer state. Reading is
not answering, and closing without an answer grants no authorization. Neither
call sends an additional notification. Rewriting `to_user.md` cannot overwrite
publications or pending questions; it remains an editable overview.

The independent composer service combines agent candidates, explicit suggestion
feedback and cross-agent Memory. Suggestions remain editable and send only on
user submission. See [[gateway/agents/docs/reply-suggestion/reply-suggestion.meiva.okf.md]].

The other modes are `naive` (ordinary assistant text) and `prefix` (explicit
visibility markers, the compatibility default). Ask is available in every mode;
publish is available in SDK mode. Mode is frozen per agent and follows the
ordinary config/restart lifecycle.

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

## Compatibility

The agent-facing `notify`, `edit_notice` and `dismiss_notice` entries are removed.
Ask uses the existing question store and answer receipts, preserving historical
question IDs and answers. Pending legacy questions are migrated without expiry.
Internal legacy routes remain for existing producers; SDK mode refuses new
legacy notice creation. System-alert delivery remains a separate capability.

## Key Dependencies

- [[ui/web/src/docs/frontend.meiva.okf.md]] — Timeline, publications and questions.
- [[meiva_builtins/plugins/meiva_fleet/docs/notice-questions.meiva.okf.md]] — question answers.
