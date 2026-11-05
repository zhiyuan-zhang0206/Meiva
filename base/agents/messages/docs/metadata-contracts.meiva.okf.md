---
type: doc
title: Message metadata contracts
description: "The cross-stack wire contracts of the message metadata layer: the closed NoteTag set, the compact summary header, and the background-wake user-reply prefix."
tags: []
---

# Message metadata contracts

The load-bearing wire contracts of `base/agents/messages/` (`kwargs.py` and
`reply_intent.py`) — the places the `meiva_*` metadata vocabulary and reply
routing prefix leave that package and other components (agent, gateway,
im_bridge, insights, frontend) depend on them.

Parent: [[base/agents/messages/docs/kwargs.meiva.okf.md|Message Metadata Contract]].

## Compact summary header

`COMPACT_SUMMARY_HEADER` opens every replacement compact summary. Since the
summary travels as one system note (`compose_summary_message` wraps header and
summary in `wrap_system_note`), the header is the note's first line rather than
the message's first characters. `is_compact_summary_text` is the one classifier
for it: it accepts the wrapped form and the legacy `[system] …` opening of a
summary stored before the note carried tags, so the context breakdown keeps
bucketing old history correctly.

## Assistant text visibility

New assistant text blocks use an exact first-line marker, followed by a newline
and a non-blank body. `MEIVA_DISPLAY_TO_USER` puts the body in the main
conversation for direct answers and meaningful user-facing updates.
`MEIVA_HIDE_FROM_USER` puts the body in Details, which the user can expand and
read. Missing or malformed markers preserve the raw text in Details and trigger
a `reply_visibility` system note for the model. The native driver puts tool
response corrections after the tool results, and retries a malformed terminal
reply once. The external harness driver sends the correction as a separate turn
with a distinct idempotency key and at most one retry.

`meiva_explicit_visibility` marks new checkpoint output. Native chat SSE carries the corresponding `explicit_visibility` field.
External harness activity snapshots carry the projected body and `user_facing`. Historical output without the marker retains
its wake-based projection. A fresh background wake includes a silent reminder
with the current protocol; wake source no longer decides new text visibility.
The frontend generates the marker constants from
`base/agents/messages/reply_intent.py`.
