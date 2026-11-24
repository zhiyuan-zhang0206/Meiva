# Ops Surfaces (cluster admin + stats dashboard)

## Cluster admin endpoints

Gateway-only, for ssh-free ops on a deployed cluster.

- **`GET /api/cluster/admin/events`** — query the telemetry and log events (Loki, the last
  seven days) with `agent_id` /
  `service_only` (skips agent-process rows) / `level` / `since` (ISO timestamp) /
  `event` (loguru event-name match) / `grep` (substring of the recorded line) / `limit`.
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
`gateway/tests/routers/cluster_endpoints/test_admin_events.py` stores lines in a Loki stub and
checks the query the endpoint sends and the rows it returns; [event reads](../../events/docs/event-reads.md)
says how each filter is written in LogQL.

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
| windowed `tokens`, `cost_usd`, `subscription_cost_usd`, `openai_cost_usd`, `anthropic_cost_usd`, `google_cost_usd` | `llm_usage_ledger` (Postgres) |
| `plugin_stats` (plugin-declared cards) | `plugin_stats` rows (`base/packages/plugins/surface/stats.py`), only rows of cards an enabled plugin declares in `contributions.ui.stats` / `statsFace` (an orphan row is not served; the console still joins the rest by `(plugin, id)`); NOT windowed — a plugin value is a point in time |

Every request computes its window in one pooled connection with an 8-second
statement timeout (a timeout is a retriable 503): one grouped scan of the window's
`llm_usage_ledger` rows (`gateway/cluster/monitor/stats_events.py`) gives the
token sums, the metered API cost, the subscription-covered cost and per-model metered
API cost slices, one count of unresolved `alerts` rows (`ops/alerts.py`) gives the
live alert summary. `openai_cost_usd`, `anthropic_cost_usd`, and `google_cost_usd` resolve each model's
vendor through the gateway `ModelCatalog` (`None` only when catalog startup failed,
preserving `200` for the rest of the dashboard). The window is the requested one
(up to 30 days), so `applied_window_hours` equals `window_hours`. The `llm_usage_ledger.cost_usd`
sum is the usage-time quote snapshot. Billing mode splits the totals. `cost_usd`
reports API list-price USD. `subscription_cost_usd` reports the same list prices
as virtual USD for subscription usage. Both are served every time — the console renders `$0.00` rather than hiding a zero
half, because "no subscription call in this window" is an answer the reader asked for.
Every read is scoped to the current home-derived cluster label (rows with no label included).

`GET /api/agents/{id}/inspect/statistics` owns window-dependent cost, tokens, calls and cache hit.
It reads `llm_usage_ledger` in one statement over `[start, end)` of `occurred_at`; the cumulative
view starts at the agent's birth. Turn counts, exec outcomes, durations, TPS and activity time are
not served. No runner probe, current-state projection, heartbeat query, or Loki scan occurs here.

`GET /api/agents/{id}/inspect/live` exclusively owns the current projection,
notice, runner shell probe, and indexed recent-pause lookup from the durable
Postgres trail. It never queries Loki. An unavailable runner sets
`shells_available=false`. These reads load,
render, fail, and retry independently. The old mixed `/inspect` route is absent.

## Key Dependencies

- [Gateway Routers](../../routers/docs/routers.md) — the router index these two belong to
- [Logging](../../../base/log/docs/log.md) — the emitter that fills the unified `events` stream, and its partitioning
