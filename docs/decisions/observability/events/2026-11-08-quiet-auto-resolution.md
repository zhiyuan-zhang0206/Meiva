# A quiet warning/error class is resolved; the Status numbers count only unresolved ones

## Decision

A warning/error class `(level, event_name, source, process)` stops counting as unresolved when
nothing of it was recorded for `GatewayConfig.alert_class_quiet_seconds` (default one hour), or
when an active `event_dismissals` row cancels it. `services/upkeep/events_maintenance/resolution.py`
owns the rule (`is_quiet`, `resolution_state`); the gateway applies it on read, so no new state
table, no migration, and no daemon pass is needed — the answer is derived from the class's
`last_seen` on every request.

The Stats popover and the dashboard's `warnings` / `errors` count only unresolved classes;
`warnings_resolved` / `errors_resolved` carry the other side so a reader can account for what
disappeared. `GET /api/stats/alert-classes` lists unresolved classes by default and returns the
resolved ones (each marked `resolved_by`) only for `include_resolved=true`.

Because the rule is derived, recurrence reopens a class by itself: an event after the quiet
window makes the class unresolved again on the next read. A manual dismissal is different —
it keeps the class hidden until the burst valve reopens it or an operator reopens it — because a
person's decision outranks a derived one.

## Consequences

- The user-visible numbers answer "what is happening now" instead of "what happened in the last
  N hours" (2026-11-08 user ruling: a warning that is not continuing worries the user for
  nothing).
- The window (`hours`) and the quiet rule are separate: the window bounds how far back a class
  can be found, the quiet rule decides whether it still needs attention. A 24-hour window can
  therefore list a class only if it recorded something within the quiet window.
- The Grafana gauges are unaffected: the events-maintenance pass publishes them over a fixed
  six-hour window, where a class quiet for the whole window is absent already.
- `machines.up_since_at` is deliberately not part of the rule. `base/cluster/machines.py` states
  it is a display-only boot stamp that no decision path reads, and `telemetry_events`'s anomaly
  index does not carry `machine`, so a per-machine generation test would turn the hottest read
  path's index-only scan into heap reads.
- The quiet window is a `GatewayConfig` field, so a cluster that needs a different tolerance sets
  it where the other stats knobs live; it is not a per-request parameter.
