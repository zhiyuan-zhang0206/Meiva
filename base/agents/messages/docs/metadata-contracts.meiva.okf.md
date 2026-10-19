---
type: doc
title: Message metadata contracts
description: "The cross-stack wire contracts of the message metadata layer: the closed NoteTag set and the compact summary header."
tags: []
---

# Message metadata contracts

The load-bearing wire contracts of `base/agents/messages/kwargs.py` — the
places the `meiva_*` metadata vocabulary leaves that module and other components
(agent, gateway, insights, frontend) depend on it.

Parent: [[base/agents/messages/docs/kwargs.meiva.okf.md|Message Metadata Contract]].

## Compact summary header

`COMPACT_SUMMARY_HEADER` opens every replacement compact summary. Since the
summary travels as one system note (`compose_summary_message` wraps header and
summary in `wrap_system_note`), the header is the note's first line rather than
the message's first characters. `is_compact_summary_text` is the one classifier
for it: it accepts the wrapped form and the legacy `[system] …` opening of a
summary stored before the note carried tags, so the context breakdown keeps
bucketing old history correctly.
