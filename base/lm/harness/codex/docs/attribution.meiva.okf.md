---
type: doc
title: Who an app-server notification belongs to
description: '`base/lm/harness/codex/` — attribution: the turn id the adapter binds from `turn/started` or `turn/start`, why a name it never bound has no owner, and what a completion or error this session did not run becomes.'
tags:
- base
- library
- llm-inference
- codex
---

# Who an app-server notification belongs to

The app server runs work this session did not start — a queued message, a review,
a compaction, and, across `thread/resume`, the tail of a turn an earlier session
ran. Every turn-scoped notification therefore goes through the shared
`_claim(turnId)` and never through "there is a turn in flight, so it is ours":
the adapter binds the provider's name once (`turn/started` when it beats the
`turn/start` response, the response otherwise) and a name it never bound has no
owner. `threadId` is checked too: one connection serves one thread, and a
notification naming another one is not this session's work.

| Notification | Becomes |
|---|---|
| `turn/started` while this session is starting a turn | the name bound to that ticket; at any other moment, nothing |
| `turn/completed` naming our turn | `TurnCompleted` |
| `turn/completed` naming any other turn | `UnsolicitedOutput(source="turn/completed")` carrying that turn's reply |
| `error` with `willRetry=false` naming our turn | `TurnFaulted` |
| `error` with `willRetry=false` naming any other turn | `UnsolicitedOutput(source="error")` |
| any turn-scoped notification whose `threadId` is another thread | nothing |

`UnsolicitedOutput` is recorded and handed to the session's sink; codex sessions
are opened without one, so such a report is recorded, not delivered — the runtime
has nothing here it must answer. A transport close is `SessionFailed`, the shape
that says the session died rather than a turn.

The rule and the three events are [[base/lm/harness/docs/attribution.meiva.okf.md|Who
a provider message belongs to]]; `tests/test_unsolicited.py` pins the codex half.
