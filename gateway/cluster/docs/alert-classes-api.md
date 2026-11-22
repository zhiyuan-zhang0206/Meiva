# Diagnostics — the release-aware anomaly/error read

Parent: [Ops Surfaces](ops-surfaces.md).

Diagnostics (`GET /api/stats/diagnostics` and `/api/stats/diagnostics/samples`) are a
windowed, release-aware telemetry summary kept strictly separate from formal Grafana Live
Alerts (`alerts` table). Formal alerts own the red badge and Insights/Control alert views;
diagnostics never drive alert badges and require no manual dismissal state.

## Admission and archiving

- **Strict admission**: groups one diagnostic class per `(level, event_name, source, process)`
  over `telemetry_events` rows that are either `level IN ('error', 'critical')` (excluding
  non-`log` events declared `tier="noise"`) or `level = 'warning'` for events explicitly
  declared `tier="anomaly"` in `base/events/declarations/`. Unregistered warnings, bare
  `event_name='log'` warnings, and `tier="noise"` warnings are excluded.
- **Release-aware archiving (`deployed_sha`)**: every emitted telemetry row carries the
  process's `deployed_sha`. Comparing against the gateway's current `process_image.sha`,
  a class with no occurrences on the current release auto-archives (`archived_by="deployed_sha"`).
  A class that also occurred on an older release and recurs on the current release stays
  active with `regressed=true`.
- **Quiet fallback**: within the current release (or when `current_sha` is empty), a class
  with no occurrence for `GatewayConfig.diagnostic_quiet_seconds` (default one hour)
  auto-archives (`archived_by="quiet"`).

Only active classes are listed unless `include_archived=true`. The dashboard's
`diagnostics` object reports `active_classes`, `archived_classes`, `active_events`,
`archived_events`, `errors` (error + critical events in active classes), `warnings`
(warning events in active classes), and `deployed_sha`. The list is
most-frequent-first, capped at 200 rows. `/samples?level&event_name&source&process&hours`
returns the newest five events of one class (`attributes.msg` as `message`, plus
`deployed_sha` and full attributes). Both reads run under an 8-second statement timeout
(a timeout is a retriable 503).

## Key dependencies

- `gateway/cluster/monitor/diagnostics.py` — the read that admits, groups, and archives the window's diagnostic rows
- [Ops Surfaces (cluster admin + stats dashboard)](ops-surfaces.md) — the dashboard aggregate whose `alerts` and `diagnostics` fields separate formal alerts from diagnostics
