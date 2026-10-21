---
type: doc
title: Browser session restore
description: What a deliberate stop records about the shared Chrome's tabs, how the next start brings them back, and what the ledger may never contain.
tags: [browser, lifecycle]
---

# Browser session restore

A `meiva stop --stop-browser` ends the headed Chrome, and the tabs open in it are
the user's work. Since the browser runs on one dedicated, persistent profile
(`$MEIVA_HOME/chrome-profile/`), the *logins* always survived a stop; this
feature makes the **tabs** survive it too.

## Two halves: the ledger, and Chrome's own session

`services/desktop/browser/session_restore.py` writes a ledger at the top of the
stop and reads it back at the next start:

1. **The ledger** (`<home>/run/browser-session.json`, mode 0600, published
   atomically beside the other lifecycle state) records, per tab: URL, title,
   pinned state, tab-group title, strip index and window index, plus the capture
   time. It never records a cookie, token, request header or body. It also
   carries the handshake field `pending` (`restore` → `layout` → absent).
2. **Chrome restores the tabs.** The ledger asks for it and nothing else: the
   `meiva-browser` daemon adds `--restore-last-session` when, and only when, the
   ledger has a pending restore. Chrome then reopens the windows and tabs, in
   their order, on the same profile.

The recorded pin and group are **re-applied** afterwards by the `browser-mcp`
daemon (one extension call, `meivaApplyLayout`), because Chrome's own restore is
not reliable about them — see below. That pass is idempotent and only ever pins
and groups: it never closes, moves or unpins a tab.

## What was measured (Chrome 156.0.8078.47, macOS)

With a throwaway profile and the SIGTERM a `meiva stop` sends:

- SIGTERM is a **clean** exit: `profile.exit_type` becomes `SessionEnded` and
  `Default/Sessions/Session_*` is written.
- Relaunching that profile with `--restore-last-session` restored the URL set and
  its order **9 of 9** times, and the pinned tab plus the tab group **8 of 9**.
  A launch always adds its own `about:blank` page; the ledger and the self-proof
  line ignore it, and nothing here ever closes it.
- The "continue where you left off" preference (`session.restore_on_startup: 1`)
  is **not** a substitute: on its own it restored 2 of 4 tabs and dropped both
  tabs that were in a group. Do not reach for it.
- `kill -9` leaves `Preferences` unwritten (or an older `exit_type` behind), so
  Chrome's own exit marker can never identify a crash. The ledger's `pending`
  field does, and only a deliberate stop writes it: **a crash or a watchdog
  respawn never restores**.

## Why the browser's own session is not the whole answer

CDP cannot express a pin, a tab-group membership or a strip position
(`Target.createTarget` has no such parameter, and the protocol has no tab-group
domain at all — see [per-agent tab groups](tab-groups.meiva.okf.md)). So a
"rebuild the tabs myself" design cannot meet a pinned/grouped acceptance bar
either, and it would put every tab's existence behind an extension call. Letting
Chrome restore the tabs and repairing only the two attributes it sometimes drops
keeps the failure mode small: the worst case is "every URL is back, the pin is
not", never a lost tab.

## The self-proof line

`meiva start` prints one line, in two independent counts:

```
  browser session: restored 9/9 tabs; pin/group reapplied 2/2
```

- `restored N/M` — recorded URLs open now / recorded, matched as a multiset so a
  duplicated URL counts twice. `about:blank` never counts on either side.
- `pin/group reapplied K/J` — layout attributes re-applied / recorded. It says
  `pin/group not recorded (the worker was unavailable at stop)` when the stop
  could not reach the tab-group worker, and `pin/group reapply still running`
  when the daemon's pass had not finished within the report wait.

The line is printed once per restore: `report_pending` is cleared after it.

`pin/group reapplied K/J` counts **satisfied** attributes, not changes: a pin
Chrome restored itself still counts, because the number answers "how many of the
recorded pins and grouped tabs are in place".

## What does not set up a restore

- `--keep-browser`: the browser is never stopped, so nothing is recorded and no
  ledger is written.
- A crash, a `kill -9`, or the watchdog relaunching a dead Chrome: no `pending`
  field, so the launcher passes no flag.
- `meiva start` that does not launch the browser (e.g. `--only-service gateway`):
  the ledger stays `pending: restore` for the start that does.
