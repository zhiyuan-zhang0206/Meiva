# Alerts Router

The Alert system (user design 2026-08-12): Alert is fully separate from
Question — own table, own UI section; nothing here touches `agent_questions`. Grafana's embedded Alertmanager evaluates the alert rules
(`deploy/lgtm/config/grafana/provisioning/alerting/rules.yml` as code) and delivers the Alertmanager
standard webhook payload to the gateway; this router is the other half of
the loop. This router is the only way alerts enter the store: no other process writes
`alerts` (`tests/scripts/test_alert_single_path.py`); every other condition is an
event plus a Grafana rule
([decision](../../../docs/decisions/observability/alerts/2026-10-04-alerting-on-grafana-alerting.md)).
One row per episode. The store core lives in `base/telemetry/alerts/__init__.py`. Four HTTP surfaces plus one background
reconciler:

- `POST /api/alerts` — the webhook (Grafana embedded-Alertmanager contact
  point, `deploy/lgtm/config/grafana/provisioning/alerting/contact.yml`). Upserts each alert instance
  into `alerts` and publishes it on the SSE channel. It delivers nothing: the IM
  fan-out retired with the IM channels on 2026-11-08.
- `POST /api/alerts/agents/{agent_id}` — an optional Alertmanager contact point that queues one
  message per notification group in the selected agent inbox.
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

Response: `{processed, inserted, updated}`.

### Lost-resolution reconciliation

With Grafana admin auth configured, the events-maintenance service's startup + five-minute loop resolves
stored Grafana instances absent from Grafana's current Alertmanager truth.
Exact identity, race boundaries, failure posture, and the rejected timestamp
sweep: [Alert Resolution Reconciliation](alert-reconciliation.md).

### SSE stream

Every ingested row is published to the Redis channel `meiva:alerts` as one
`AlertRow` JSON frame; `GET /api/alerts/stream` forwards the channel in
broadcast mode through the same `event_stream` machinery as agent events
(heartbeat + error frames, reconnection-safe). A Redis outage never fails
the ingest — the publish is best-effort and the initial fetch covers the
gap.

### Alertmanager agent receiver

The built-in Grafana provisioning routes to the alert store. A site can add
`POST /api/alerts/agents/{agent_id}` as a second contact point. It accepts the
same webhook authentication as ingest and returns HTTP 204 after inbox
acceptance. The URL selects the recipient. Use an active on-duty agent so it can
act on the usual inbox wake. A terminated agent's system message remains queued
for a later life.

Copy the [complete example](../../../deploy/lgtm/config/grafana/contact.agent.example.yml)
to `$MEIVA_HOME/lgtm/alerting/contact.yml` on the Grafana station. Replace
`1234` with the recipient agent ID and run the normal LGTM converge. Grafana
loads the rendered file at startup. For a running Grafana, reload it through
the admin API at `POST /api/admin/provisioning/alerting/reload`.
[Grafana provisioning reload](https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/api-legacy/admin/#reload-provisioning-configurations)

The example uses the station's existing webhook URL and Bearer token environment references.
Keep the agent route first with `continue: true` to retain the existing ingest
routes. The example sends error and critical notifications, including resolved
groups, and inherits the root grouping and timers. The override replaces the
whole contact and policy file. Use the example's webhook receiver and policy
structure. Each receiver needs a unique UID, a webhook URL and a named contact
point. The root policy and any explicit child receiver must reference a
contact point in the file for the same Grafana organization. Specify routing
matchers as `object_matchers` triples as shown in the example. Use the common
Alertmanager regex syntax, including character classes, alternation and ordinary
repetition. Write policy durations in descending Prometheus units, within the
signed 64-bit nanosecond range. Write booleans as `true` or `false`, and give
each mapping key a single definition.

Environment references are scoped to the rendered Grafana runtime values.
`MEIVA_ALERTS_WEBHOOK_URL` is available in the webhook URL and
`MEIVA_ALERTS_WEBHOOK_TOKEN` in the authorization credential. Use
`$__env{NAME}`, `${NAME}`, or `$NAME`. The token can be empty for loopback
delivery. Routing labels, matchers and timers use literal values. Grafana
reduces `$$` to one literal dollar. Converge validates the expanded
values and publishes the original YAML, keeping credential references in the
provisioned file.

Keep the override as a regular UTF-8 YAML file of at most 256 KiB. The example
is a few KiB and this bound leaves room for station policies while limiting the
work before converge can proceed. Converge refuses links in the override path
and validates its structure before replacing the installed copy or its hash.
Keep any local changes in the source file and compare it with the shipped
policy when upgrading.

To stop agent routing, remove the override, converge and reload alerting
provisioning. Grafana may retain the unused contact point. The restored policy
routes to ingest only.

The receiver formats the instances in the POST into one message with statuses,
summaries, diagnostic labels, Grafana rule links and event query commands.
Auth, validation and inbox failures return non-2xx. The pinned Grafana notifier
ends a failed attempt, with future sends determined by Alertmanager's subsequent
group evaluations. An ambiguous replay can duplicate a message because a
webhook group key also identifies intentional repeats. See the
[delivery decision](../../../docs/decisions/observability/alerts/2026-11-24-alertmanager-agent-receiver.md).

### Shadow transition facts

The alert-specific renderer preserves its localized text in the transition group.

The ingest transaction freezes immutable revision/group/member snapshots through
`base.telemetry.alerts.shadow.AlertShadowBatch`; fingerprint gates preserve
input-order instance resolution. Repeated observations retain the original group.
`notification_revision` advances on each frozen transition; `notified_at` has no
writer left and stays NULL. The Grafana label `notify_im: "false"` is inert: it gated a
delivery, the ingest does not read it any more, and a rule that carries it records its
transitions like every other rule. Shadow history is never proof of non-delivery and
cannot be automatically dispatched. See
[Alert Notification Shadow Facts](alert-shadow-facts.md) for transaction ownership and the retained history boundary.

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
