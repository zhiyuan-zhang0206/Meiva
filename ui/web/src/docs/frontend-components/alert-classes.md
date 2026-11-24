# Alerts Boundary

Meiva keeps no alert state or alert view of its own: Grafana / Alertmanager own rules,
grouping, silences, recovery and history (user ruling 2026-11-24). Live Grafana alerts stay
off the sidebar Status popover (user ruling 2026-11-22), and there is no self-built
diagnostics view: error classes are read in Grafana / Loki.

- **Firing count**: the sidebar footer bell badge (and the collapsed rail's restore button
  and the phone menu button) shows what `GET /api/alerts/firing` returns, polled every 30s
  (`useFiringAlerts`, `lib/notifications/use-firing-alerts.ts`). The gateway reads Grafana's
  Alertmanager on demand and counts active, unsilenced, uninhibited instances without the
  synthetic watchdog heartbeat. The badge is hidden at zero.
- **Unknown is not zero**: `state: "unavailable"` (Grafana did not answer) renders a grey
  "?" of the same size as the count bubble, labelled "Grafana unreachable, alert status
  unknown". `state: "not_configured"` (no Grafana upstream) renders nothing.
- **Click-through**: the bell opens Grafana's firing-alert list
  (`/grafana/alerting/list?search=state:firing`, through the gateway's proxy) in a new tab.
  `/insights#ops` carries the same link next to the ops dashboard link, and the old
  `/insights/alerts` URL redirects there. The collapsed-rail bubble is only a marker on the
  restore button and is not a link.
- **Freshness**: the count is at most 30s old; there is no alert SSE stream.
