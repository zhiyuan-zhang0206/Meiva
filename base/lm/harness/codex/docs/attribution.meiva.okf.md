---
type: doc
title: Who an app-server notification belongs to
description: '`base/lm/harness/codex/` — attribution: the authoritative turn id from `turn/start`, how early notifications are buffered, and where foreign completions and errors go.'
tags:
- base
- library
- llm-inference
- codex
---

# Who an app-server notification belongs to

The app server runs work this session did not start — a queued message, a review,
a compaction, and, across `thread/resume`, the tail of a turn an earlier session
ran. The response to `turn/start` names the authoritative turn. An early
`turn/started` can supply an interrupt target. Early activity waits for the
response and is then dispatched after `ForegroundAccepted`. Only activity
naming the authoritative turn contributes to the running foreground. Each
connection serves one thread.

During launch, notifications without a turn ID or the matching thread ID
cannot be attributed. The early buffer applies `max_buffered_events` to text
and reasoning deltas while retaining control notifications. A rejected launch
discards the buffer.

| Notification | Becomes |
|---|---|
| `turn/started` while this session is starting a turn | provisional interrupt target |
| `turn/start` response naming a turn | authoritative turn binding and `ForegroundAccepted` |
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
