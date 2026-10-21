---
type: doc
title: Notify — questions requiring a user response
description: Response-required notices with independent priority and blocking attributes, three question types, and transactional replies.
tags:
- fleet
- notify
- user-communication
---

# Notice contract

A notice always requires a user response. Ordinary information, progress and
results belong in chat; there is no FYI queue or mark-read action.

Two independent attributes describe each notice:

| Attribute | Meaning |
|---|---|
| `priority` | P0 (highest), P1, P2 (default), P3 (lowest) |
| `blocking` | Whether the agent cannot progress until the answer arrives |

Any priority can be blocking or nonblocking. The queue sorts by priority, then
blocking, then oldest first. A nonblocking notice still needs an answer.

## Creation and editing

`meiva.ui.notify(title, content=None, *, require_response=True, blocking=False,
priority='P2', task=None, expire_at=None, idempotency_key, question=None)` posts
one notice. An agent holds one open notice at a time: while one is open, a second
`notify` is refused with that notice's id and the two ways out — `dismiss_notice`
to close it, `edit_notice` to rewrite it in place — and nothing is replaced on the
writer's behalf (user ruling 2026-10-21). The refusal is a `ValueError` carrying
the gateway's sentence. `require_response=False` is rejected before any effect;
it is not an optional obligation.

The title names the question and content supplies background. `question` is
`text`, `single_select` or `multi_select`; see [[notice-questions.meiva.okf.md]].
Omitting it keeps the legacy free-text answer surface. Choice questions accept
an optional comment alongside their selected options. New Other options are
rejected. The task must exist, the title must be nonblank, and invalid priorities
fail at the boundary.

Retain the required idempotency key across retries and use an absolute expiration
deadline. The returned integer `Notice` includes `.pending_count` and
`.pending_notices`.

`edit_notice` changes the current notice's priority or blocking status. A structured
question freezes its title, background and option order. `dismiss_notice` withdraws
the observed current notice. Both use observed identity and durable receipts, so
retrying cannot affect a replacement notice.

## Replies and history

The user answers or explicitly closes without answering. Answering stores the
selection and comment together and delivers one `system:notice-reply` inbound.
Closing delivers `system:notice-dismiss` and grants no authorization. Lost-response
retries reuse their operation key and replay the original acceptance.

The live feed, inspector and IM queue contain only response-required rows. The
feed's legacy `open` field stays empty; `awaiting` is the active question queue.
Resolved notice history also excludes FYIs. Historical FYI rows and read receipts
remain stored for compatibility; they are not converted into unanswered questions.
The schema's legacy fields are retained rather than destructively rewritten.

## Expiry

A notice carries an absolute `expire_at`. When it passes with the notice still
open, the TTL reaper resolves it as `expired` and queues one system inbound
(`system:notice-expire`) for its agent in the same transaction: the local id, the
title, when it lapsed, that nothing is pending any more, and that the single open
notice is free again. The message is not shaped as a reply, because the user did
not answer.

The owner is woken only while running or idling. A terminated owner keeps the
same row in its inbox for its next life, and is never woken or resurrected for
it. Expiry is silent for the user (user ruling 2026-10-21): the question leaves
the queue and no notification is posted in its place.

## Owners

- [[notice-questions.meiva.okf.md]] — question, selection and comment semantics.
- [[gateway/agents/docs/notice-receipts.meiva.okf.md]] — transactional acceptance.
- [[meiva/docs/ui.meiva.okf.md]] — SDK surface.
