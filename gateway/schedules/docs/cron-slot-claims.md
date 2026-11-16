# Cron slot claims

This helper belongs to compatibility daemon payloads. Declarative cron schedules
store their expression and next eligible fire on the schedule row; they use an
open `schedule_runs` row as the single-flight claim instead.

- Compatibility cron knowledge remains in each resident script template.
- Online fires call `fire_slot_once()`; a slot missed while the schedule was not running is not replayed. The TTL reaper deletes every claim past the retention window.
- `fire_slot_once()` claims with `INSERT ... ON CONFLICT DO NOTHING`, which commits before the callback, making `(schedule_id, slot_fire_at)` at-most-once across concurrent processes and restarts.
- The winning callback receives `(slot_fire_at, payload)` explicitly; the slot is normalized to UTC before claiming and dispatch. Window-based reports use that slot.
- A crash after the claim but before callback completion leaves the slot claimed. This accepted loss window is the explicit cost of at-most-once behavior; there is no at-least-once retry path.

## Relationship to Other Nodes

- [Schedule Manager & Runner](schedules.md) — the overview of the resident-schedule supervisor this mechanism belongs to.
