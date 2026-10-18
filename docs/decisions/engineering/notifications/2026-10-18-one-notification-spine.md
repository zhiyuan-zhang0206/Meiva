# One delivery decision behind every notification transport

Date: 2026-10-18

## Context

Four surfaces tell the user something happened: the console inbox, the Android
app, macOS, and the Telegram bridge. Each decided *when* to interrupt on its own:
the app's injected bridge watched agent status and treated busy-to-idle as
"finished", the Telegram bridge filtered `agent_notices` by priority, the console
showed every notice, and macOS had no surface at all. Adding a fifth transport
meant writing that judgment again, and a plugin had no way to say "this event is
worth interrupting for" — its only lever was posting a notice, which is an
answerable inbox item, not a ping.

Notices (`agent_notices`) are stateful: answerable, superseded, expiring, one
open row per agent. A "turn finished" ping is none of those. Reusing the notice
table for it would have meant exempting categories from supersede and from the
inbox — special cases in the one table the user reads as their queue.

## Decision

A separate, small delivery spine owns the decision:

- **Category** — id, label, importance, default switch, dedupe window. Core
  categories live in `base/events/notifications/catalog.py`; a plugin declares its
  own in `contributions.notifications` under its `<plugin>.` prefix.
- **Delivery log** — `notification_events`, append-only: one row per delivered
  notification, never answered or superseded.
- **Policy** — exactly two suppressions: the category is switched off, or the same
  `(category, dedupe_key)` fired inside the category's dedupe window. A null key
  opts out of deduplication. Suppression is silent and returns no row, so a
  producer has nothing to retry.
- **One live event** — `notification` on the fleet-wide broadcast carries the row
  just written; every transport renders it, and a reconnecting client deduplicates
  on `notification_id`.

Defaults follow the same conservative rule: a category that fires on every turn of
every agent ships switched off (`core.turn_finished`), and a notice delivers again
when its question changes, because a new question is work for the user right now.

Producers: a hosted turn that ran to completion (`core.turn_finished`, emitted on
the turn path, not the settlement path that also fires for no-work parks), a
posted notice that needs an answer (`core.needs_input`, read from the committed
row and keyed by the notice id plus a fingerprint of its question, so adding a
question delivers again), and `meiva_fleet.task_closed` through the plugin
endpoint. A notice that needs no answer notifies nobody: the console inbox already
carries it, and a ping the user cannot act on is noise.

## Consequences

Easier: a new transport renders `subject` and stops. A new plugin declares a
category and calls one endpoint. The user gets one switch per category that every
transport honors, and the delivery log answers "did we tell them, and when".

Harder: two stores now describe user-facing events, and the boundary has to stay
honest — an answerable question is a notice, a ping is a notification. The
console's `SystemEvent` union is closed, so a new role needs a frontend fixture in
the same change.

## Alternatives considered

- **Extend `agent_notices` with a category column and transient rows.** Fewer
  moving parts, and Telegram already reads that table. Rejected: it would make
  supersede, expiry and the inbox read conditional on the category, turning one
  clean table into two behaviours behind a flag.
- **Let plugins call the platform notification API directly.** Rejected: it
  bypasses the user's switches, has no deduplication, and puts a transport
  decision in third-party code. The contribution surface exists so a plugin can
  declare *what matters*, never *how to interrupt*.
- **A per-category, per-transport matrix.** Rejected for one user with four
  transports: N×4 switches to maintain, and no evidence anyone wants "Telegram on,
  phone off" for the same category.
