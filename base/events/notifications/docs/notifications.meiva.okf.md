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
notification as a side effect, and a notice that needs no answer posts nothing —
the inbox already carries it.

## Categories

A category carries a label, an importance, a default switch and a dedupe window.
Core categories live in `catalog.CORE_CATEGORIES`. `core.turn_finished` ships
switched off: a fleet-wide "turn finished" ping is dozens of interruptions an
hour, so it is one the user opts into. `core.needs_input` ships on. A plugin declares its own in
`contributions.notifications` in `meiva-plugin.json`, under its own
`<plugin>.` id prefix; `gateway/extensions/notification_categories.py` merges the
manifests with the core set, and `GET /api/notifications/categories` returns that
merged view with the user's switches already applied.

## Policy

A notification is suppressed when its category is switched off, or when a
delivery with the same `(category, dedupe_key)` happened inside the category's
dedupe window. A null `dedupe_key` opts out of deduplication. Suppression is
silent and returns no row.

## Producers

A hosted turn that ran to completion records `core.turn_finished` from
`services/agent_runner/agent_host/host.py`; a crashed, cancelled or truncated turn
says nothing, because the notification lives on the turn path rather than on the
settlement path that also fires for no-work parks. A posted notice
records `core.needs_input` from `ops/lifecycle/events.py`, keyed by the notice
id plus a fingerprint of its question, so a retried post stays silent while
editing a container to add a question delivers again — a new question is what the
user has to act on. The spine reads the notice row itself rather than trusting
either post path to pass the right shape.

## Plugin emission

A plugin delivers through `POST /api/agents/{id}/notifications` with
`category`, `subject` and an optional `dedupe_key`; the endpoint resolves the
category against the merged catalog (422 when undeclared), writes the delivery
and announces it. `meiva_fleet` registers `meiva_fleet.task_closed` and emits it
from the task update path after the row commits, keyed by task id. It ships
switched off like `core.turn_finished`: the category fires once per closed task,
which measured 227 closes (37.8 a day, 27 in the busiest hour) over 2026-10-12..18,
64% of them agents closing their own bookkeeping and 42% between 23:00 and 06:00.
The console task board already shows every close, so the ping is an interruption
the user opts into rather than one they must switch off.

## Transports

`ui/app/src-tauri/scripts/notify-bridge.js` renders the delivered event on
Android and macOS. The console shows every delivery through its own inbox, and
the Telegram bridge still reads `agent_notices` directly — it moves onto these
rows next. No transport re-derives its own trigger from agent state. A transport
may render less (an importance floor, a master switch), never more.
