# Alerts Boundary

Meiva keeps no alert state or alert view of its own: Grafana / Alertmanager own rules,
grouping, silences, recovery and history (user ruling 2026-11-24). Live Grafana alerts stay
off the sidebar Status popover (user ruling 2026-11-22), and there is no self-built
diagnostics view: error classes are read in Grafana / Loki.

- **Firing count**: the sidebar footer bell badge, the collapsed rail's restore button and
  the phone menu button show the sum of `critical` and `error` from `GET /api/alerts/firing`,
  polled every 30s by `useFiringAlerts`. The gateway counts active, unsilenced, uninhibited
  instances and excludes the synthetic watchdog heartbeat. The API also reports `warning`
  and the aggregate `firing` count for diagnostics. The badge is hidden at zero.
- **Unknown is not zero**: `state: "unavailable"` (Grafana did not answer) renders a grey
  "?" of the same size as the count bubble, labelled "Grafana unreachable, alert status
  unknown". `state: "not_configured"` (no Grafana upstream) renders nothing.
- **Click-through**: the bell opens Grafana's Active notifications page
  (`/grafana/alerting/groups`, through the gateway proxy) in a new tab. The link filters
  for active instances with `severity=~"critical|error"` and groups them by alert name
  and machine so the group header shows the resolved host label when present.
  `/insights#ops` carries the same link next to the ops dashboard link, and the old
  `/insights/alerts` URL redirects there. The collapsed-rail bubble is only a marker on the
  restore button and is not a link.
- **Freshness**: the count is at most 30s old; there is no alert SSE stream.
