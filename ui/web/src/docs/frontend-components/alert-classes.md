# Alerts Boundary

Live Grafana alerts stay off the sidebar Status popover (user ruling 2026-11-22).
There is no self-built diagnostics view: error classes are read in Grafana / Loki.

- **Live Alerts**: surfaced only when `unresolved > 0` via the sidebar footer bell
  badge (and collapsed rail restore button) and the full list at `/insights#alerts`.
  `GET /api/alerts` and the SSE stream own ordering, filters, and uncapped counts;
  all unresolved instances remain visible regardless of age while the history
  window selects recently resolved episodes by recovery time. Grouping and
  recovery belong to Grafana / Alertmanager, not frontend time buckets.
