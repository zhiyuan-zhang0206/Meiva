---
type: doc
title: Notification spine
description: One delivery decision for every notification transport.
tags: [contracts]
---

# Notification spine

A notification is one request that needs the user's reply. Nothing else is one:
a finished turn, a closed task or any other event the user does not have to
answer belongs in the log, the task board or the inbox that already carries it.

`base/events/notifications` owns that delivery decision: producers name a
category, the spine applies the user's per-category switches, deduplicates by
key, records the delivery, and publishes one live event every transport
consumes.

## Store

`notification_events` holds one row per delivered notification. It is a delivery
log, not an inbox: rows are never answered, resolved or superseded. The inbox is
`agent_notices`; a notice that needs an answer stages a `core.needs_input`
delivery in its own transaction, and a notice that needs no answer stages
nothing — the inbox already carries it.

The table is also the outbox. `announced_at` is when the live event was
published; a NULL means the delivery is still owed. A producer stages the row
when it decides (a notice's row commits with the notice, so a crash between the
commit and the publish leaves a deliverable debt rather than a silent loss), and
`announce_sync` marks the row only after the transport accepted the publication — a
known Redis failure returns None and marks nothing. A publish with no subscriber
is accepted too: Redis took the message and nobody was listening, so the row is
marked delivered and a client that was offline gets it from the recovery read,
not from the live event. The outbox closes the failures the transport reports,
never the silence of an empty subscriber list. The gateway sweeps
`announced_at IS NULL` rows older than one sweep interval
(`notification_outbox_interval_seconds`) and re-announces them
(`notification_outbox_batch` bounds one pass). A row younger than an interval is
left to the producer still working inside its request. Re-announcing after a
lost mark is at-least-once on purpose: clients deduplicate on `notification_id`,
and a duplicate ping is cheaper than a missing decision.

## Categories

A category carries a label, a description, an importance, a default switch and a
dedupe window. `catalog.CORE_CATEGORIES` holds exactly one: `core.needs_input`,
a notice that needs an answer, and it ships on. A plugin declares its own in
`contributions.notifications` in `meiva-plugin.json`, under its `<plugin>.` id
prefix; a plugin category ships switched off — `defaultEnabled: true` is a
manifest error — and the user turns it on.
`gateway/extensions/notification_categories.py` merges the manifests with the
core set, and `GET /api/notifications/categories` returns that merged view with
the user's switches already applied.

## Policy

A notification is suppressed when its category is switched off, or when a
delivery with the same `(category, dedupe_key)` happened inside the category's
dedupe window. A null `dedupe_key` opts out of deduplication. Suppression is
silent and returns no row.

## Producers

A notice that needs an answer stages `core.needs_input` inside the transaction
that writes it — `gateway/agents/notices.py` and
`gateway/agents/notice_operations/current.py` on the gateway path — keyed by the
notice id plus a fingerprint of its question, so a retried post stays silent
while editing a container to add a question delivers again: a new question is
what the user has to act on. The staged row also carries the notice's own route
in `href` (`/fleet?notice=<id>`, the inbox route that selects a row), so a
transport that can open a link opens *that* notice instead of a queue the user
then has to search. `ops/lifecycle/events.py` publishes only the inbox's
live hint. The spine reads the notice row itself rather than trusting either
post path to pass the right shape.

A completed turn and a closed task are not notifications. The agent host logs
the finished turn (`services/agent_runner/agent_host/host.py`) and the task
registry logs the closed task
(`meiva_builtins/plugins/meiva_fleet/task_registry.py`); both remain queryable
through the settled agent row, the task board and the task's `task_updated`
event, and neither asks the user for an answer.

The task-maintenance daemon's stall escalation is the same producer on a sync
path: its user leg commits a `require_response` notice and stages the
`core.needs_input` delivery inside the escalation transaction, then announces it
after the commit, so a stall that needs a human decision reaches the push
transports as `core.needs_input` instead of stopping at the console inbox — and
an announce lost to a crash stays owed for the sweep. Its delegator leg is an
agent note and deliberately produces no user notification.

## Plugin emission

A plugin delivers through `POST /api/agents/{id}/notifications` with
`category`, `subject` and an optional `dedupe_key`; the endpoint resolves the
category against the merged catalog (422 when undeclared), writes the delivery
and announces it. A declared category names a request that needs the user's
reply and ships switched off: enabling it is the user's switch, never the
plugin's default.

## Auditing a window

`meiva notices audit` replays the producer stream for a window (`--days`, 7 by
default; `--now` and `--tz` place its end and its day/hour buckets) through this
policy. It reads the producers rather than `notification_events` because the log
holds only what was delivered — a switched-off category has no rows to count, and
a switch cannot be simulated from the rows its own policy dropped.
The replay lives in `audit.py` and the gateway serves it as
`GET /api/notifications/audit` (`days`, `category`, `tz`, `now`, repeated `simulate`,
`explain`; `tz` defaults to the cluster clock's zone); the CLI only renders the
returned report.

- `core.needs_input` comes from `agent_notices` rows that need a response. One
  row is one candidate, or two when the agent added a question after posting: the
  spine keys a re-ask by the question the notice carries now, so the post is keyed
  question-less and the edit by its question.

One simulation is modelled: keying a notice by `notice:<id>` instead of
`notice:<id>:<question fingerprint>` (the re-ask switch).

## Transports

`ui/app/src-tauri/scripts/notify-bridge.js` renders the delivered event on Android and
macOS, and catches up after a gap through `GET /api/notifications?after_id=`: the live
event is a Redis publication with no memory, so the delivery log is the only thing a
client that was offline can ask. A publish with no subscriber is still a successful
publish — Redis accepts it and the row is marked delivered — so this read, not the live
event, is what makes a delivery survive an offline client. The endpoint returns the
newest deliveries after the cursor (oldest-first) plus the newest id, so a first-run
client adopts a cursor instead of replaying history; every later advance uses the last
row it actually read. The console renders the per-category switches that decide those
deliveries (Display settings → Notifications, the `notifications.categories` map) and no
delivery feed of its own: its inbox is the notice queue, so the `notification` role
never enters a timeline. The Telegram bridge still reads `agent_notices` directly for
what to push — it moves onto these rows next — but it applies the same per-category
switches before pushing, so a decision notice the user switched off does not reach
Telegram either. Historical FYI rows receive filtered receipts and are never
pushed; ordinary information stays in conversation. No transport
re-derives its own trigger from agent state. A transport may render less (an importance
floor, a master switch), never more.
