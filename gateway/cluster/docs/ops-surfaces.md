# Ops Surfaces (cluster admin + stats dashboard)

## Cluster admin endpoints

Gateway-only, for ssh-free ops on a deployed cluster.

- **`GET /api/cluster/admin/events`** — query the unified `events` stream with `agent_id` /
  `service_only` (skips agent-process rows) / `level` / `since` (ISO timestamp) /
  `event` (loguru event-name match) / `grep` (message substring) / `limit`.
  Answers "what did the labeler say in the last 10 minutes" without ssh.
- **`DELETE /api/cluster/machines/{name}`** — drop a stale `machines` row (a
  one-off bench host that is never coming back). **Refuses to delete the
  caller's own row** (`machine_name()` on the gateway process), so a cluster
  cannot delete itself out of its own roster. Idempotent: a name that does not
  exist returns 204.

Neither has an SDK wrapper. They are HTTP-only, called directly over the private
network, because they are operator tools and the SDK surface is for agents.

Admin events request only telemetry/log categories and treat `level` as a
minimum. The limit defaults to display configuration; omitting `since` uses a
24-hour floor. `gateway/cluster/admin_events_plan.py` turns the request's filters
into the exact query (or a refusal answered as 400), a pure function whose rules
`gateway/cluster/tests/test_admin_events_plan.py` asserts on the returned plan;
`gateway/tests/routers/cluster_endpoints/test_admin_events.py` seeds real
`telemetry_events` rows and checks which of them the endpoint returns.

## The roster: plan, then shell

`GET /api/cluster/roster`, `/api/cluster/machines` and the `/api/status` cluster panel read
the same roster. What is done for each machine is a pure plan
(`gateway/cluster/roster/roster_plan.py`): the gateway's own machine without the agent-runner
capability reads itself, a machine with a fresh heartbeat snapshot renders from it
(`gateway/cluster/roster/liveness_snapshot.py`), and every other machine, or every runner on a
`fresh=true` read, is dialed. The row shapes are `gateway/cluster/roster/roster_rows.py`. The shell
(`gateway/cluster/status.py`) reads the machines table, the snapshots and the local facts, then
executes the plan; a dial is a real `status_probe` round trip to the row's registered URL under
`gateway.status_probe_timeout_seconds`. Tests serve a runner on a real listener
(`gateway/tests/support/runner_ops`) and assert on the plan and on the rows.

The cluster router exposes no source-checkout update, rollout, restart or
update-check endpoint. The status page displays observations and does not
launch deployments; a fleet update is `meiva update`.

## `GET /api/stats/dashboard?hours=`

Backs the status cards of the frontend sidebar's Status popover in **one** round trip,
polled on one page-wide 30s cadence regardless of how many sidebar consumers
are mounted. `hours` is the aggregation window, whitelisted to
0 (5m) / 1 / 6 / 24 / 72 / 168 / 720 (default 24).

| Card data | Source |
|---|---|
| `live_count` | Postgres `agents_meta` (not windowed) |
| `alerts` (`unresolved`, `critical`, `error`, `warning`) | Postgres `alerts` (`status = 'unresolved'`, excluding synthetic `alert_pipeline_watchdog` rows — NOT windowed; single source of truth shared with `/api/alerts`, the header badge, and Insights Alerts) |
| windowed `tokens`, `cost_usd`, `subscription_cost_usd`, `openai_cost_usd`, `anthropic_cost_usd`, `google_cost_usd` | `telemetry_events` (Postgres) |
| `diagnostics` (`active_classes`, `archived_classes`, `active_events`, `archived_events`, `errors`, `warnings`, `deployed_sha`) | `telemetry_events` grouped by `(level, event_name, source, process)` under strict diagnostic admission (`error`/`critical` plus explicitly declared `tier="anomaly"` warnings), auto-archived across `deployed_sha` releases and 1h quiet window |
| `plugin_stats` (plugin-declared cards) | `plugin_stats` rows (`base/packages/plugins/surface/stats.py`), only rows of cards an enabled plugin declares in `contributions.ui.stats` / `statsFace` (an orphan row is not served; the console still joins the rest by `(plugin, id)`); NOT windowed — a plugin value is a point in time |

Every request computes its window in one pooled connection with an 8-second
statement timeout (a timeout is a retriable 503): one grouped scan of the window's
`llm_usage` rows (`gateway/cluster/monitor/stats_events.py`) gives the
token sums, the metered API cost, the subscription-covered cost and per-model metered
API cost slices, one count of unresolved `alerts` rows (`ops/alerts.py`) gives the
live alert summary, and one grouped count of admitted diagnostic rows
(served by the partial index `telemetry_events_anomaly_ts`) gives
the diagnostic summary. `openai_cost_usd`, `anthropic_cost_usd`, and `google_cost_usd` resolve each model's
vendor through the gateway `ModelCatalog` (`None` only when catalog startup failed,
preserving `200` for the rest of the dashboard). The window is the requested one
(up to 30 days), so `applied_window_hours` equals `window_hours`. The `llm_usage.cost_usd`
sum is the usage-time quote snapshot, not historical tokens repriced against today's
registry. The two cost sums are disjoint halves of the same rows, split by the billing
mode of each row's `source`: `cost_usd` is the fee an API call incurred, `subscription_cost_usd`
the list value a call under `SUBSCRIPTION_HARNESSES` would have cost and the subscription
covered. Both are served every time — the console renders `$0.00` rather than hiding a zero
half, because "no subscription call in this window" is an answer the reader asked for.
Every read is scoped to the current home-derived cluster label (rows with no label included).

The diagnostic class reads have their own node: [Diagnostics — the release-aware anomaly/error read](alert-classes-api.md)
— strict admission, `deployed_sha` auto-archiving, and the quiet fallback that decides which classes are active.

`GET /api/agents/{id}/inspect/statistics` owns window-dependent cost, stats,
TPS and activity. It reads one repeatable-read Postgres snapshot over persisted
observations, day summaries and disjoint historical ledger days. Exact percentile
work scales with selected duration observations; additive work uses day summaries
and at most two raw boundary days. Up to four in-flight leaders are admitted and
identical callers share their read; completed results have no TTL. Each SQL query
has a two-second statement limit and the HTTP wait has a 15-second limit. Collection
is explicitly observed, never certified lossless. Missing historical sections and
unknown compact boundaries are unavailable, with declared coverage/precision and
observation timestamps. No runner probe, current-state projection, heartbeat query,
or synchronous Loki scan occurs here.

`GET /api/agents/{id}/inspect/live` exclusively owns the current projection,
notice, runner shell probe, and indexed recent-pause lookup from the durable
Postgres trail. It never queries Loki. An unavailable runner sets
`shells_available=false`. `/inspect/widgets` owns plugin extensions. The three reads load,
render, fail, and retry independently. The old mixed `/inspect` route is absent.

## Key Dependencies

- [Gateway Routers](../../routers/docs/routers.md) — the router index these two belong to
- [Logging](../../../base/log/docs/log.md) — the emitter that fills the unified `events` stream, and its partitioning
