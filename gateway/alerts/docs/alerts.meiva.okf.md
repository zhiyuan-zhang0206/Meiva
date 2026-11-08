---
type: doc
title: Alerts Router
description: "POST /api/alerts + GET /api/alerts + GET /api/alerts/stream — the system→human alert store (alerts), Grafana-truth resolution reconciliation, the alert-section history list and the SSE live tail (Task #1224)."
tags:
- gateway
- alerts
---

# Alerts Router

The Alert system (user design 2026-08-12): Alert is fully separate from
Notice — own table, own UI section; nothing here touches `agent_notices`. Grafana's embedded Alertmanager evaluates the alert rules
(`deploy/lgtm/config/grafana/provisioning/alerting/rules.yml` as code) and delivers the Alertmanager
standard webhook payload to the gateway; this router is the other half of
the loop. This router is the only way alerts enter the store: no other process writes
`alerts` (`tests/scripts/test_alert_single_path.py`); every other condition is an
event plus a Grafana rule
([decision](../../../docs/decisions/observability/alerts/2026-10-04-alerting-on-grafana-alerting.md)).
One row per episode. The store core lives in `base/telemetry/alerts/__init__.py`. Three HTTP surfaces plus one background
reconciler:

- `POST /api/alerts` — the webhook (Grafana embedded-Alertmanager contact
  point, `deploy/lgtm/config/grafana/provisioning/alerting/contact.yml`). Upserts each alert instance
  into `alerts` and publishes it on the SSE channel. It delivers nothing: the IM
  fan-out retired with the IM channels on 2026-11-08.
- `GET /api/alerts` — the alert section's unresolved-first history list +
  unresolved count for the top-bar badge.
- `GET /api/alerts/stream` — SSE tail (channel `meiva:alerts`, broadcast) of
  every ingest; the UI's initial fetch covers rows ingested before the
  subscription opened.
- Grafana reconciliation — on events-maintenance service start and every five minutes, fetch
  Grafana's current Alertmanager instances and resolve stored rows
  absent from that truth set. This closes the lost-RESOLVE-webhook gap.

## Store — `alerts`

One row = one alert instance, deduped by **(fingerprint, starts_at)**
(unique constraint, migration `20260813T042527_alerts`). Alertmanager may
re-send the same instance while firing and sends it once more on resolution;
the upsert updates the row instead of duplicating it.
Columns: `status` (unresolved|resolved — no ack state) /
`severity` (critical|warning|error, read from the rule's `severity` label,
normalized — anything else defaults to warning) / `alertname` / `labels` /
`annotations` (jsonb, the Alertmanager shape) / `starts_at` / `ends_at` /
`fingerprint` (Alertmanager-standard fnv-1a over sorted labels, computed
when the payload omits it) / `generator_url` / `source` (`grafana`; rows
written before 2026-10-04 also carry the retired in-process writers' tags) /
`notified_at` / timestamps.
Index: `(status, starts_at DESC)` serves the list path.

## Contract

### Ingest (Alertmanager webhook)

Body = the Alertmanager standard webhook payload: `{status, alerts: [{
status, labels, annotations, startsAt, endsAt, fingerprint, values,
generatorURL}]}` — the full v4 envelope (version/groupKey/receiver/
commonLabels/… ) and the slimmer Grafana-managed shape are both accepted
(extra fields tolerated; a missing per-alert status falls back to the
top-level one). Webhook `firing` maps to store `unresolved`. Alertmanager's
zero time (`0001-01-01T00:00:00Z`) in `endsAt` is stored as NULL.

Auth — the webhook cannot hold the cluster secret, so the ingest path
bypasses the session/bearer middleware and authenticates itself:
`X-Alerts-Token` == the webhook token
(`MEIVA_ALERTS_WEBHOOK_TOKEN` — constant-time), or a cluster Bearer (the human secret or the active write generation's machine API token, which in-cluster posters present), or — only when no token is
configured — loopback trust (the single-box default: Grafana is co-located).

Response: `{processed, inserted, updated, notified}`.

### Lost-resolution reconciliation

With Grafana admin auth configured, the events-maintenance service's startup + five-minute loop resolves
stored Grafana instances absent from Grafana's current Alertmanager truth.
Exact identity, race boundaries, failure posture, and the rejected timestamp
sweep: [[alert-reconciliation.meiva.okf.md]].

### SSE stream

Every ingested row is published to the Redis channel `meiva:alerts` as one
`AlertRow` JSON frame; `GET /api/alerts/stream` forwards the channel in
broadcast mode through the same `event_stream` machinery as agent events
(heartbeat + error frames, reconnection-safe). A Redis outage never fails
the ingest — the publish is best-effort and the initial fetch covers the
gap.

### No delivery

The gateway delivers alerts nowhere. The IM channels, the native acceptance
route and the outbound worker were removed on 2026-11-08 (user ruling: the whole
IM surface goes), so `notified_at` stays NULL, the ingest answers
`processed`/`inserted`/`updated` only, and the store, the list and the SSE tail
are the whole of the alert path.

The rendered notification text survives as the transition record: the template
(severity head + summary + generatorURL + `→ <fleet UI>/insights/alerts`,
recovery swapping the head for the resolved variant) is frozen into
`alert_notification_groups` by `base.telemetry.alerts.notify_group_text`, with
zh/en variants chosen from `user_settings` `display.language` (default zh) by
`base.telemetry.alerts.display_language` at ingest time. Alert
labels/annotations are never translated. Reconciliation repairs the durable
store and the SSE view.

### Shadow transition facts

The ingest transaction freezes immutable revision/group/member snapshots through
`base.telemetry.alerts.shadow.AlertShadowBatch`; fingerprint gates preserve
input-order instance resolution. Repeated observations retain the original group.
`notification_revision` advances on each frozen transition; `notified_at` has no
writer left and stays NULL. The Grafana label `notify_im: "false"` is inert: it gated a
delivery, the ingest does not read it any more, and a rule that carries it records its
transitions like every other rule. Shadow history is never proof of non-delivery and
cannot be automatically dispatched. See
[[alert-shadow-facts]] for transaction ownership and the retained history boundary.

### List

`GET /api/alerts?window=1h|6h|24h|7d&status=&severity=&limit=`
→ `{alerts: [row...], meta: {window, total, unresolved_count}}`, ordered
`(status = 'unresolved') DESC, starts_at DESC`. `unresolved_count` backs the
top-bar badge (all unresolved instances in the severity scope, 0 when scoped to resolved).
Unresolved instances never age out. The window limits resolved history by
`ends_at` (falling back to `starts_at` for legacy rows without a recovery time).
SSE frames invalidate the frontend's alert snapshots: the API owns exact counts
and ordering even when a page is capped or an updated instance is absent from it.
GET and
stream sit behind the normal session/Bearer middleware (the UI + SDK).
