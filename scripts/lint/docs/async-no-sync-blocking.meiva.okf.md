---
type: doc
title: Async-no-sync-blocking lint
description: A synchronous psycopg, subprocess, psutil or filesystem call inside an `async def` freezes the gateway's single event loop — what scripts/lint/async_no_sync_blocking.py flags, its scope, its hook and its inline opt-out.
tags:
- scripts
- lint
---

# Async-no-sync-blocking lint

`scripts/lint/async_no_sync_blocking.py` (pre-commit `lint-async-no-sync-blocking`) keeps
blocking work out of the gateway's event loop. The gateway is a single event
loop; one sync psycopg / subprocess / psutil call inside an `async def` handler
freezes every other request (the 2026-08-03 incident: `/api/memory/search` ran a
synchronous embedding on the loop and the gateway hung for hours).

Flags, inside an `async def` body (not a nested `def` — presumed threaded):
DB connection/cursor/execute/fetch/commit/rollback calls, known sync DB
helpers and sync ops, process/session backend calls, filesystem calls,
`shutil.`/`subprocess.`/`psutil.` module calls, `os.system`, `time.sleep`,
and any bare `*_blocking`-suffixed helper (those exist to be wrapped in
`asyncio.to_thread`). Inline opt-out: `# async-blocking-ok: <reason>`.

Scope: `gateway/` and `ops/` (the event-loop surfaces).

Parent: [[scripts/lint/docs/lint.meiva.okf.md|scripts/lint]].
