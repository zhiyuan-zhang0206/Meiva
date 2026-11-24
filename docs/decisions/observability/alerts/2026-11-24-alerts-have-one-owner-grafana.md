# Grafana owns alert state; the gateway keeps no copy

## Context

Grafana's embedded Alertmanager evaluates the rules and knows, at any moment, which alerts are
firing, silenced, inhibited or recovered. The gateway also kept its own copy: an `alerts` table fed by
the webhook, a five-minute reconciliation loop that closed rows whose resolve notification was lost,
a shadow of notification transitions, an SSE channel, and a Web UI list that read the table. A
copy of state that someone else owns needs a reconciler for as long as it lives. Each of those pieces
existed to repair the one before it: the table to hold the webhook, the reconciler for a lost resolve,
the shadow to make the transitions provable. The seven-day inventory had hundreds of rows no
one read, and a Grafana that was down left stale red counts in the badge. The user confirmed on
2026-11-24 that the Web UI builds no alert view of its own: only a "N alerts firing" badge that opens
Grafana.

## Decision

- Alert state has one owner: Grafana's embedded Alertmanager. The gateway reads it on demand
  (`GET /api/alerts/firing`, server side, 2-second budget, active and unsilenced instances only,
  the watchdog excluded) and keeps nothing. The badge polls it every 30 seconds; `meiva stats` and
  `/api/stats/dashboard` call the same function.
- A Grafana that cannot be asked is `unavailable`, not zero. The badge turns grey with a question
  mark and `meiva stats` prints `unavailable`. A dead Grafana is reported by the external sentinel, not
  by the badge.
- History is Grafana's: its alerting history page and the `annotations` / `rules/history` APIs. The
  Web UI has no alert list; Insights loses its Alerts section.
- The dead-man switch keeps its path. The watchdog still posts to the gateway, which now only moves the
  heartbeat gauge (`alerts_last_ingest_timestamp_seconds`) and stores nothing; R28 and the host
  sentinel are unchanged. The built-in contact point and the `POST /api/alerts` route stay, so a
  site policy that still posts every alert there keeps working through the rollout and a rollback.
- The table goes in two steps: this change stops every writer and reader (webhook ingest, the
  reconciliation loop, the schedule hooks, the SSE channel, the list and the stats count); a later
  migration drops `alerts`, `alert_notification_groups`, `alert_notification_members` and
  `alert_escalations`.

## Alternatives rejected

- **Browser reads Grafana through the `/grafana` proxy.** The proxy is off by default, and "which
  alerts count" (watchdog excluded, severity normalized, what unreachable means) would then live
  in both the UI and the CLI. One server-side reader gives both the same answer.
- **Keep the table as a cache and drop only the UI list.** It keeps the reconciler alive, and a cache
  that is allowed to be stale is the failure being removed.
- **Move the watchdog to an external prober now.** It is the target shape, but it needs a receiver
  outside the gateway and a rollout there. Keeping the gateway receiver costs no new dependency and
  keeps the rule and the sentinel as they are.
- **A new `/api/alerts/heartbeat` route.** It would break every site policy that still points at
  `/api/alerts` at the moment of release.

## Consequences

- A silenced or inhibited alert no longer counts: the badge says what Grafana would notify about.
- Freshness is up to 30 seconds, not the instant an SSE frame arrived.
- A schedule that is stopped or deleted no longer resolves its alerts at once; they clear when the rule
  stops reporting the series. The badge can over-count for that interval.
- `meiva stats --json` changes shape: `alerts` is `{state, firing, critical, error, warning}` with
  null counts when the state is not `ok`.
- A stale browser tab that asks the merged event stream for the retired `alerts` channel is refused with
  422 until it reloads.
- Rolling back to the previous release restores the old routes; the tables are still there until the
  drop migration lands.

<!-- Supersedes: 2026-08-23-alert-ingest-reconciliation.md, 2026-10-14-native-recovery-and-event-statistics.md (the badge and list), and the gateway-side store of 2026-10-04-alerting-on-grafana-alerting.md and 2026-10-04-one-im-per-alert-group.md -->
