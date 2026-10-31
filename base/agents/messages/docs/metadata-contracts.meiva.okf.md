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

## Background-wake user-reply header

`USER_REPLY_HEADER` (`"Meiva agent to user:"`) and `USER_REPLY_PREFIX`
(`"Meiva agent to user: "`) in `base/agents/messages/reply_intent.py` disambiguate
proactive user-facing replies from internal work-log text on runs woken without
human input (`meiva_non_human_wake=True`). Both `scripts/codegen/dump_frontend_constants.py`
and `base/agents/history/timeline.py` read from `reply_intent.py`, keeping live
SSE stream folding (`ui/web/src/lib/fold/timeline.ts`), committed `TimelineItem`
projection (`user_facing`), and `agents_meta.last_message_text` normalization
aligned on one constant owner.
