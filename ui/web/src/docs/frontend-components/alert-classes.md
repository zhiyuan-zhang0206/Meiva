# Alerts and Diagnostics in the Status Popover

The Status popover (`agent-sidebar/footer.tsx`) keeps formal Grafana Live Alerts and
windowed telemetry Diagnostics strictly separate:

- **Live Alerts (top section, not windowed)**: displays `alerts` (`unresolved`, `critical`,
  `error`, `warning`) from `GET /api/stats/dashboard`, which reads the same unresolved non-watchdog
  `alerts` table rows as `/api/alerts`, the header badge, and Insights Alerts. Clicking
  navigates to `/insights#alerts`.
- **Diagnostics (Usage section, windowed)**: displays `diagnostics` (`active_classes`, `archived_classes`,
  `active_events`, `archived_events`, `errors`, `warnings`, `deployed_sha`) over the selected window. `errors` folds `critical` events;
  `warnings` includes only explicitly declared `tier="anomaly"` events. Classes from
  earlier releases (`deployed_sha`) or quiet for `diagnostic_quiet_seconds` auto-archive
  without manual dismissal. Diagnostics never drive alert badges.

The backend contract: [Diagnostics](../../../../../gateway/cluster/docs/alert-classes-api.md).

Formal Grafana alerts have their own header badge and Insights history. Their
SSE stream invalidates TanStack Query snapshots, so the server owns ordering,
filters and uncapped counts. All unresolved instances remain visible regardless
of age; the history window selects recently resolved episodes by recovery time.
Grouping and recovery belong to Grafana / Alertmanager, not frontend time buckets.
