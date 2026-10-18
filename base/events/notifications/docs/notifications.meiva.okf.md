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

## Producers

A settled hosted turn records `core.turn_finished` from
`agent/ownership/hosted.py` after its status write commits. A posted notice
records `core.needs_input` or `core.notice` from `ops/lifecycle/events.py`,
keyed by the notice id so an edit or a retried post cannot notify twice; the
spine reads the notice row itself rather than trusting either post path to pass
the right shape.

## Transports

`ui/app/src-tauri/scripts/notify-bridge.js` renders the delivered event on
Android and macOS. The console shows every delivery through its own inbox, and
the Telegram bridge still reads `agent_notices` directly — it moves onto these
rows next. No transport re-derives its own trigger from agent state. A transport
may render less (an importance floor, a master switch), never more.
