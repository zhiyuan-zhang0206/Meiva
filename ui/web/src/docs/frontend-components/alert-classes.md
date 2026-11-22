# Alerts and Diagnostics Boundaries

Live Grafana alerts and internal telemetry diagnostics stay separated and off the
sidebar Status popover (user ruling 2026-11-22):

- **Live Alerts**: surfaced only when `unresolved > 0` via the sidebar footer bell
  badge (and collapsed rail restore button) and the full list at `/insights#alerts`.
  `GET /api/alerts` and the SSE stream own ordering, filters, and uncapped counts;
  all unresolved instances remain visible regardless of age while the history
  window selects recently resolved episodes by recovery time. Grouping and
  recovery belong to Grafana / Alertmanager, not frontend time buckets.
- **Diagnostics**: structured error/anomaly classes in `telemetry_events`
  (`deployed_sha` scoped, auto-archived on release cutover or after
  `diagnostic_quiet_seconds`, with no manual dismissal) remain available through
  `GET /api/stats/diagnostics` and `meiva stats` for debugging, and never feed
  alert badges or clutter the Status popover.

The backend contract: [Diagnostics](../../../../../gateway/cluster/docs/alert-classes-api.md).
