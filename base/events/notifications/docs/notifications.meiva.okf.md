---
type: doc
title: Notification spine
description: One delivery decision for every notification transport.
tags: [contracts]
---

# Notification spine

`base/events/notifications` owns one question: does this event deserve to interrupt the
user, and on which channel? Producers name a category; the spine applies the
user's per-category switches, deduplicates by key, records the delivery, and
publishes one live event every transport consumes.

## Store

`notification_events` holds one row per delivered notification. It is a delivery
log, not an inbox: rows are never answered, resolved or superseded. The inbox is
`agent_notices`; a notice that needs an answer posts a `core.needs_input`
notification as a side effect.

## Categories

A category carries a label, an importance, a default switch and a dedupe window.
Core categories live in `catalog.CORE_CATEGORIES`; plugins declare theirs on the
`notifications` contribution surface and own the `<plugin>.` id prefix.

## Policy

A notification is suppressed when its category is switched off, or when a
delivery with the same `(category, dedupe_key)` happened inside the category's
dedupe window. A null `dedupe_key` opts out of deduplication. Suppression is
silent and returns no row.

## Transports

Android, macOS, the console and the Telegram bridge all consume the delivered
event; none of them re-derives its own trigger from agent state. A transport may
render less (an importance floor, a master switch), never more.
