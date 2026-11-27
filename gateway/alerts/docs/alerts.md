# Alerts Router

Grafana owns alert state. Its embedded Alertmanager evaluates the alert rules
(`deploy/lgtm/config/grafana/provisioning/alerting/rules.yml` as code) and decides what is firing,
silenced, inhibited or recovered; the gateway stores none of it. Every other condition is an event
plus a Grafana rule
([decision](../../../docs/decisions/observability/alerts/2026-11-24-alerts-have-one-owner-grafana.md)).
The gateway has three alert surfaces:

- `GET /api/alerts/firing` — live counts by severity for the Web UI badge and diagnostics.
- `POST /api/alerts` — the receiver of the alert pipeline's dead-man switch (the built-in Grafana
  contact point). It stores and delivers nothing.
- `POST /api/alerts/agents/{agent_id}` — an optional Alertmanager contact point that queues one
  message per notification group in the selected agent inbox.

History is Grafana's: the alerting history page (`/grafana/alerting/history`),
`GET /api/annotations?type=alert` and `GET /api/v1/rules/history?ruleUID=...` (one call returns at
most 100 rows; page with `to`). The Web UI badge sums `critical` and `error` and links to
those active instances in Grafana in a new tab.

## Contract

### Firing alerts

`GET /api/alerts/firing` → `{state, firing, critical, error, warning}`. The gateway asks Grafana's
embedded Alertmanager for its active instances
(`/grafana/api/alertmanager/grafana/api/v2/alerts?active=true&silenced=false&inhibited=false`,
anonymous Viewer, 2-second budget) and counts them by their `severity` label, excluding the
synthetic watchdog (`metric=alert_pipeline_watchdog`). A `severity` that is not `critical`, `error`
or `warning` counts as a warning. A silenced or inhibited instance, and one still inside its rule's
`for` window, does not count.

`state` says how far the counts can be trusted:

| `state` | Meaning | Counts |
|---|---|---|
| `ok` | Grafana answered | exact |
| `unavailable` | Grafana is configured but did not give a usable answer (down, error status, not JSON, not an alert list) | `null` |
| `not_configured` | the gateway's Grafana proxy is off, so there is no Grafana upstream | `null` |

A reader shows `unavailable` as unknown and never as zero: a dead Grafana cannot be told from a quiet
cluster by a count. The route sits behind the normal session/Bearer middleware and never returns an
error status for a Grafana failure. `/api/stats/dashboard.alerts` carries the same object, and
`meiva stats` prints it (`alerts: unavailable (grafana unreachable)` when it cannot count).

### Alert webhook (the heartbeat receiver)

Body = the Alertmanager standard webhook payload: `{status, alerts: [{status, labels, annotations,
startsAt, endsAt, generatorURL}]}`. The full v4 envelope (version/groupKey/receiver/commonLabels/…)
and the slimmer Grafana-managed shape are both accepted (extra fields tolerated; a missing per-alert
status falls back to the top-level one). A firing alert labelled `metric=alert_pipeline_watchdog`
emits the `alerts` telemetry event, which moves the gauge `alerts_last_ingest_timestamp_seconds`
that the dead-man rule (`meiva-ops-alert-pipeline-silent`) and the host sentinel read. Anything else
in the payload is acknowledged and dropped. Response: HTTP 204.

Auth — the webhook cannot hold the cluster secret, so the route bypasses the session/bearer
middleware and authenticates itself: `X-Alerts-Token` == the webhook token
(`MEIVA_ALERTS_WEBHOOK_TOKEN` — constant-time), or a cluster Bearer (the human secret or the
active write generation's machine API token, which in-cluster posters present), or — only when no
token is configured — loopback trust (the single-box default: Grafana is co-located). A wrong
credential is 401 and moves no heartbeat.

### Alertmanager agent receiver

The built-in Grafana provisioning notifies nobody. A site adds
`POST /api/alerts/agents/{agent_id}` as a contact point. It accepts the
same webhook authentication as `POST /api/alerts` and returns HTTP 204 after inbox
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
Keep the agent route first with `continue: true` so the watchdog heartbeat route still runs. The example sends error and critical notifications, including resolved
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
routes the watchdog heartbeat only.

The receiver formats the instances in the POST into one message with statuses,
summaries, diagnostic labels, Grafana rule links and event query commands.
Auth, validation and inbox failures return non-2xx. The pinned Grafana notifier
ends a failed attempt, with future sends determined by Alertmanager's subsequent
group evaluations. An ambiguous replay can duplicate a message because a
webhook group key also identifies intentional repeats. See the
[delivery decision](../../../docs/decisions/observability/alerts/2026-11-24-alertmanager-agent-receiver.md).
