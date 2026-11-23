# Alertmanager routes notifications to the agent inbox

## Context

Grafana's embedded Alertmanager groups rule instances, applies silences and chooses when to notify.
The gateway stores those instances for the alert view and publishes updates over SSE. That store
continues to reconcile against Grafana. A site can also send operational alerts to an on-duty agent.

## Decision

- Ship the ingest-only Alertmanager configuration by default. A site may select an inbox receiver
  by replacing the full Grafana contact and policy file. The sample adds a first sibling matching
  `severity=~"critical|error"` with `continue: true` so the ingest routes still match. The November
  2026 seven-day inventory had 343 warning episodes and 54 error or critical episodes. The sample
  reserves the agent's attention for the smaller, higher-severity band.
- Alertmanager owns the destination, grouping, silences, firing and recovery notifications, and
  subsequent send attempts. The gateway accepts the webhook at `POST /api/alerts/agents/{agent_id}`
  using the existing webhook authentication and queues one inbox message per successful POST. It
  renders the structured instances to show their status, labels, source links and event queries.
  Alertmanager can send a group containing both firing and resolved instances. Per-instance text
  keeps that status visible and works with the standard webhook shape.
- Retire the shadow escalation loop and its age, cooldown and mode settings. The database ledger
  stays during the rollout window. [Follow-up](../../../../future/infra/data/alert-escalations-drop.md)

## Delivery boundary

The HTTP result reports inbox acceptance. An active agent receives the normal chat wake hint.
A terminated agent's system notification waits for a later life under the existing lifecycle policy.
The standard webhook supplies a group identity shared by legitimate repeats, so each accepted POST
is a message and an ambiguous replay can produce a duplicate.

An unsuccessful send surfaces as a webhook error. The pinned Grafana webhook notifier treats that
attempt as non-recoverable in the current dispatch. Later Alertmanager group evaluations may send
again under the configured policy, with no delivery guarantee for an individual recovery.
[Grafana webhook implementation](https://github.com/grafana/alerting/blob/e1a167a201a8/receivers/webhook/v1/webhook.go#L158-L162)

[Setup and rollback](../../../../gateway/alerts/docs/alerts.md#alertmanager-agent-receiver)
