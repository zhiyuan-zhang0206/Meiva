---
type: doc
title: Alert Classes — the unresolved warning/error read
description: The gateway's alert-class diagnostics (`/api/stats/alert-classes` and its `/samples`) — grouping identity, dismissal matching, and the quiet window that decides which classes still need attention.
tags:
- gateway
- ops
---

# Alert Classes — the unresolved warning/error read

Parent: [[gateway/cluster/docs/ops-surfaces.meiva.okf.md|Ops Surfaces]].

The sidebar's Warnings / Errors card displays the dashboard's unresolved event totals,
separately from formal Grafana alerts. The diagnostic API groups one class per
`(level, event_name, source, process)` of the window, with its event count, first and
last occurrence, the `dismissal_id` of the active `event_dismissals` row that cancels it
(None = no dismissal) and `resolved_by`: `"dismissed"` for that row, `"quiet"` when the
class recorded nothing for `GatewayConfig.alert_class_quiet_seconds` (default one hour),
None while it is unresolved. Only unresolved classes are listed unless
`include_resolved=true` (see [quiet auto-resolution](../../../docs/decisions/observability/events/2026-11-08-quiet-auto-resolution.md)). The dismissal match is
`resolution.matching_dismissal` — the daemon's: the emission category is ignored, an
exact `process` row or a wildcard row (`process = ''`, task #4329 B5) cancels the class,
and the exact row wins when both match. A reopened or per-agent row cancels nothing. The
dashboard's `alert_classes_active` / `alert_classes_dismissed` count the unresolved and
resolved classes, and its `warnings` / `errors` sum only unresolved ones (`warnings_resolved` /
`errors_resolved` carry the rest; critical folds into errors). The list is most-frequent-first,
capped at 200 rows (`total_classes` / `total_events` cover the returned set).
`/samples?level&event_name&source&process&hours` returns the newest five events of one
class (`attributes.msg` as `message`, plus the full attributes). Dismiss and reopen use `POST /api/event-resolutions[/{id}/reopen]`
calls, scoped per class. Both reads run under an 8-second statement timeout
(a timeout is a retriable 503).

## Key dependencies

- `gateway/cluster/monitor/alert_classes.py` — the read that groups the window's rows
- [[gateway/cluster/docs/ops-surfaces.meiva.okf.md]] — the dashboard aggregate whose `warnings` / `errors` these classes decide
- [quiet auto-resolution](../../../docs/decisions/observability/events/2026-11-08-quiet-auto-resolution.md) — why a quiet class reads as resolved
