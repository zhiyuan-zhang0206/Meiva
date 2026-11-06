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

In `user_delivery_mode=prefix` (the compatibility default), new assistant text
blocks use an exact first-line marker, followed by a newline
and a non-blank body. `MEIVA_DISPLAY_TO_USER` puts the body in the main
conversation for direct answers and meaningful user-facing updates.
`MEIVA_HIDE_FROM_USER` puts the body in Details, which the user can expand and
read. A leading fragment that can still form either marker and a marker
without a non-blank body both project to an empty Details item. Other missing
or malformed markers preserve the raw text in Details. Invalid blocks still
trigger a `reply_visibility` system note for the model. The native driver puts tool
response corrections after the tool results, and retries a malformed terminal
reply once. The external harness driver sends the correction as a separate turn
with a distinct idempotency key and at most one retry.

`meiva_explicit_visibility` marks new checkpoint output. Native chat SSE carries the corresponding `explicit_visibility` field.
External harness activity snapshots carry the projected body and `user_facing`. Historical output without the marker retains
its wake-based projection. A fresh background wake includes a silent reminder
with the current protocol; wake source no longer decides new text visibility.
The frontend generates the marker constants from
`base/agents/messages/reply_intent.py`.

## Delivery modes and durable attention

`UserDeliveryMode` in `base/api_contracts/user_delivery.py` owns `naive`, `prefix`
and `sdk`. `MEIVA_USER_DELIVERY_MODE` configures the cluster default; the frozen
per-agent `user_delivery_mode` overlay selects another mode. `naive` displays
ordinary text; `sdk` folds all assistant text into expandable Details and uses
`meiva.ui.publish` for user delivery. A publication never ends a turn.

`meiva_user_delivery_mode` records the mode as a plain string on native messages,
so checkpoint serialization needs no custom Enum type. `delivery_mode`
and `source_message_id` travel on chat SSE. Source identity keeps the durable
attention record attached to the same bubble across streaming and compaction.
Historical messages without the field retain their existing metadata semantics.

Visible native/prefix and harness messages create durable `agent_user_messages`
records. SDK publications may carry reply suggestions for the independent
composer service. Unread state clears on reading. Ask stores persistent questions
separately; their pending state clears only on answer or explicit close. These records live
independently of `to_user.md`. Pending excerpts are reinjected after compaction,
and the SDK reader retrieves complete records.

Native and harness delivery mirrors attach to loaded Timeline items without
injecting unloaded history or changing item IDs. The unread queue retains those
records across pagination and compaction. SDK publications remain independent
Timeline entries because they have no corresponding assistant-text item.

See [the delivery-mode decision](../../../../docs/decisions/engineering/notifications/2026-11-06-user-delivery-modes.md)
and [[meiva/docs/ui.meiva.okf.md]].
