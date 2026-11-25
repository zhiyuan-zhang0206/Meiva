# Grafana alerting — Meiva ops alert rules (as code)

Grafana Alerting rules for the Meiva event system, provisioned as code. Since
the LGTM cutover (Task #1224) they evaluate against the **LGTM read side**:
R1-R2, R5-R6, R13, R17, and R19 query **Loki** (every event is one OTLP log
line under `{service_name="unknown_service"}`, body = the full event JSON, so
`| json` flattens each line to labels), while R4, the gateway-metrics silence
rule, the main-red watch round heartbeat rule, and the root-health round freshness rule query **Prometheus** (the
`meiva_llm_usage_latency_milliseconds` histogram,
`meiva_gateway_latency_count_total` heartbeat,
`meiva_root_health_tick_last_tick_timestamp_seconds` and
`meiva_root_health_expected_expected_since_timestamp_seconds` gauges, and R18 turn-duration
histogram); R24 queries Prometheus's own too-old-samples counter. The retired
Postgres events read path (#1197) is gone — nothing
queries the `ops` datasource from these rules.

Datasources are provisioned beside this file (`datasources.yml`, uids
`loki` / `prometheus`; the `ops` Postgres datasource stays for the meiva-ops
dashboards).

### LogQL migration (task #1467, complete)

- Since the 2026-08-23 index-label cutover (base/telemetry/loki_index_labels.py) the
  collector promotes `event_name` / `agent_id` to stream labels; event-scoped
  rules match them INSIDE the stream selector
  (`{service_name="unknown_service", event_name=...}`) and keep `| json` only
  for the level/category/attributes filters (those fields are NOT stream
  labels).
- The legacy chunks without index labels expired after the cutover's
  retention grace (2026-08-26); promoting the filters before that expiry
  would have silently dropped the still-retained history.
- Selector matchers are full-string regexes (unlike pipeline label filters,
  which are substring searches), so multi-alternative `event_name=~"a|b"`
  matches exact event names.

### Retired rules

Grafana keeps file-provisioned rules after their definition is removed.
Record retired UIDs in the [retirement inventory](../../retired-alert-rules.json),
including any older names. The inventory lives outside the provisioning tree.
Grafana's `deleteRules` directive deletes by organization and UID regardless of
ownership, so routine converge and reload never apply this inventory directly.

To clean the station during a rollout, converge the new release and verify
that the normal rendered alerting directory carries no `deleteRules` entries.
Then execute the following maintenance sequence:

1. Stop normal Grafana and back up its database. Use the installed launcher
   with the same data directory, a fresh temporary provisioning root containing
   an empty `alerting` directory, and a private loopback listener. Disable
   anonymous access and evaluation during cleanup. For example, in a private
   terminal with `staging` and `native` set to those directories:

   ```bash
   GF_PATHS_PROVISIONING="$staging" GF_SERVER_HTTP_ADDR=127.0.0.1 \
     GF_SERVER_HTTP_PORT=53004 GF_SERVER_ROOT_URL=http://127.0.0.1:53004 \
     GF_SERVER_SERVE_FROM_SUB_PATH=false GF_AUTH_ANONYMOUS_ENABLED=false \
     GF_UNIFIED_ALERTING_EXECUTE_ALERTS=false "$native/grafana/run.sh"
   ```

2. Keep that maintenance listener exclusive to the operator. From another
   terminal, fetch the existing rule list with Grafana admin authentication.
   Select only organization 1 rules whose UID is in the retirement inventory
   and whose live provenance is `file`. Use the inventory from the new release.

   ```bash
   curl --fail --silent --show-error --user admin \
     http://127.0.0.1:53004/api/v1/provisioning/alert-rules > "$staging/live.json"
   jq --exit-status --slurpfile retired "$inventory" '
     if type != "array" then error("expected the Grafana rule list") else
       {apiVersion: 1, deleteRules: [
         .[] | select(.orgID == 1 and .provenance == "file")
         | .uid as $uid | select($retired[0] | index($uid))
         | {orgId: .orgID, uid: $uid}
       ]}
     end
   ' "$staging/live.json" > "$staging/plan.json"
   ```

3. Inspect the plan. When it has entries, move it into the temporary
   `alerting` directory as `delete-rules.json`. Reload the maintenance
   instance with `curl --fail --user admin --request POST http://127.0.0.1:53004/api/admin/provisioning/alerting/reload`.
   Verify that planned UIDs are gone and that any non-file rule remains.
   Discard the temporary provisioning root
   after stopping the maintenance instance and start the normal service.
   A subsequent normal reload or restart never sees the one-time directives.

Run `meiva observability status` on the station after reloading. It exits nonzero
and lists the UIDs present on only one side of the comparison between
rendered provisioning and Grafana, including any locally managed rules.

## Interval collector silence

The collector-silence rule was restored after the schedule runner's telemetry
binding fix was verified in production Loki. It reads every service identity:
`service_name` is not part of the collector's event contract. Both successful
and failed cycles establish liveness.

The existing 35-minute window accommodates the current slowest collector's
30-minute refresh. This is a fleet-wide dead-man signal: one collector still
emitting prevents the rule from firing. Per-collector freshness requires a
separate expected-set policy. Restoring this rule removes its UID from the
retirement inventory in the same change.

## Where these run (LGTM stack)

Native Grafana on the LGTM host (port 3003 by default, 53003 on the station via
`MEIVA_LGTM_GRAFANA_PORT`) evaluates these rules from the converge-rendered
provisioning tree (`$MEIVA_HOME/lgtm/native/config/
provisioning/`, copied verbatim by `cli/commands/observability/lgtm_native.py` from the
source checkout's `deploy/lgtm/config/grafana/provisioning` — a rendered copy
that drifted from its recorded hash is restored from the checkout and reported as
`converge_file_overwritten`, because the checkout owns this tree; the webhook URL
is a Grafana-native `$__env{MEIVA_ALERTS_WEBHOOK_URL}` reference resolved from
the process env).
The contact point posts to the gateway's alert webhook — loopback
`127.0.0.1:8000` when the observatory is local, the gateway's reachable
address when `MEIVA_OBSERVABILITY_URL` points at a remote station. The gateway keeps no
alert state: Grafana is the one truth, and the Web UI badge and `meiva stats` read it live.

## Rules

The rules are split between `meiva-ops` (evaluated every minute) and
`meiva-ops-slow` (evaluated every five minutes). Each rule retains its own
`for` window and threshold. `rules.yml` is the condition catalog: events without a
matching rule do not create an alert of their own (aggregate rules may still count
them). The Warning / Error status card counts events separately.
The table below names the configured conditions.

## Episode recovery and grouping

Grafana owns episode identity and recovery. Every rule uses the native file field
`keepFiringFor: 5m`, shared by a YAML alias in `rules.yml`: a healthy evaluation
enters Recovering, and another failure during recovery continues the same episode.
The setting does not delay the initial firing or change severity thresholds.
A real recovery followed by a later failure starts a new episode; the gateway and
frontend do not merge independent episodes by timestamp.

Missing series use Grafana's native `missing_series_evals_to_resolve` as well:
six evaluations for the 1m group and two for the 5m group retain a disappearing
instance for at least five minutes from its first missing evaluation. This avoids
a peer recovering early while another peer still returns data. NoData/OK handling
also follows Grafana's native recovery state machine. Root diagnostic queries keep
`machine`, so samples from different hosts cannot combine into a failure streak.

Alertmanager grouping and notification timing live in `contact.yml`:
`group_by: [alertname]`, initial wait 30s, group updates every 5m and unchanged
repeats every 4h; critical retains 0s / 1m / 1h. The notification interval is
separate from the recovery period. Warning and error tiers remain separate rules
with their existing pending periods.

References: [Grafana evaluation](https://grafana.com/docs/grafana/latest/alerting/fundamentals/alert-rule-evaluation/),
[file schema for the pinned Grafana](https://github.com/grafana/grafana/blob/v13.2.3/pkg/services/provisioning/alerting/rules_types.go).

## Rule conditions

The Alerts page shows a rule's summary line, so a rule about one agent groups by the envelope
`agent_id` stream label and names it there; a fleet-volume rule keeps its fleet-wide threshold and
its summary adds how many distinct agents the volume spans (a query `B` counting agent ids over the
same window, `or vector(0)`, reduced as `N`). The shared agent host binds no agent to its logger, so
an event carries an agent id only when its call site passes `agent_id=`. A rule whose event family
gained its agent id at the call site renders the label through
`{{ with $labels.agent_id }}{{ . }}{{ else }}unknown{{ end }}`, because Grafana prints a missing label
as `[no value]`. Pinned by `tests/scripts/test_alert_rules.py`.

Application layer — the Loki event stream plus the LLM latency histogram:

| uid | Group | Metric | Condition | `for` | Severity |
|-----|-------|--------|-----------|-------|----------|
| `meiva-ops-warning-error-spike` | `meiva-ops` | WARNING+ERROR+CRITICAL spike | 5m count > 3× prior-15m rate AND ≥ 30 (Loki) | 5m | error |
| `meiva-ops-sse-drop-backlog` | `meiva-ops` | sse_drop + event_log_drop backlog | SUM(payload.n) in 10m > 100 (Loki unwrap); summary adds the distinct agent count | 5m | warning |
| `meiva-ops-llm-latency-p95` | `meiva-ops` | llm_usage latency p95 | histogram p95 in 10m > 60000 ms (Prometheus) | 10m | error |
| `meiva-ops-delivery-stalled-backlog` | `meiva-ops` | delivery_stalled fresh backlog | fresh (age_s<600) count in 10m > 50 (Loki); summary adds the distinct agent count | 5m | warning |
| `meiva-ops-inbound-consumed-unknown` | `meiva-ops-slow` | inbound consumed without execution evidence | `inbound_consumed_unknown` count in 24h > 2 (Loki); summary adds the distinct agent count | 10m | warning |
| `meiva-ops-events-freshness` | `meiva-ops` | event stream stalled | no events in Loki for 5m (absent_over_time) | 5m | error |
| `meiva-ops-root-health-stale` | `meiva-ops` | root health round freshness | a machine+home+process expecting rounds within 24h has its last completed round >3m stale, or none since startup (Prometheus) | 0m | error |
| `meiva-ops-gateway-metrics-silent` | `meiva-ops` | gateway_latency heartbeat | no samples in Prometheus for 5m (absent_over_time) | 5m | error |
| `meiva-ops-main-red-watch-silent` | `meiva-ops` | main-red watch round heartbeat | no `meiva_main_red_watch_round_rounds_total` sample for 1h (two missed half-hour rounds) while one was seen in the last 6d (Prometheus, absent_over_time) | 5m | error |
| `meiva-ops-llm-billing-quota` | `meiva-ops-slow` | LLM key out of credit / quota | llm_provider_error with billing=true in 15m > 0 (Loki) | 0m | critical |
| `meiva-ops-gateway-latency-route-warning` | `meiva-ops-slow` | Gateway latency: fast route p95 | p95 > 3s for 5m (Loki, LLM-bound + inherently-slow routes excluded) | 5m | warning |
| `meiva-ops-gateway-latency-route-error` | `meiva-ops-slow` | Gateway latency: fast route p95 | p95 > 10s for 5m (same route filter) | 5m | error |
| `meiva-ops-turn-duration-p95` | `meiva-ops-slow` | Turn duration p95 (collective slowdown) | histogram p95 > 75s for 10m (Prometheus, 24h baseline 37.6s × 2) | 10m | warning |
| `meiva-ops-gw-latency-slow-warning` | `meiva-ops-slow` | Gateway latency: slow route p95 | p95 > 5s for 5m (Loki, slow route class) | 5m | warning |
| `meiva-ops-gw-latency-slow-error` | `meiva-ops-slow` | Gateway latency: slow route p95 | p95 > 10s for 5m (same route class) | 5m | error |
| `meiva-ops-tempo-backend-down` | `meiva-ops-slow` | Tempo backend reachable | up{job="tempo"}=0 for 1h (Prometheus) | 1h | warning |
| `meiva-ops-events-low-water` | `meiva-ops` | event stream below low-water mark | count in 5m < 150 (Loki) | 10m | warning |
| `meiva-ops-telemetry-queue-loss` | `meiva-ops` | telemetry queue lost events | a machine+process+queue's last drop < 300s old (Prometheus) | 0s | error |
| `meiva-ops-recovery-drill-failed` | `meiva-ops` | scheduled recovery drill failed | `recovery_drill_failed` (level=error) by drill in 1h > 0 (Loki) | 0m | error |
| `meiva-ops-backup-dump-failed` | `meiva-ops` | scheduled logical dump failed | `backup_dump_failed` (level=error) in 24h > 0 (Loki) | 0m | error |
| `meiva-ops-llm-rate-limit` | `meiva-ops-slow` | LLM provider rate-limit burst | HTTP 429s by vendor in 5m > 5 (Loki) | 0m | warning |


Infrastructure layer (issue #46) — the per-machine OTel Collector sidecar's
own scrapes carry `host_name` (OS hostname) and `machine` (Meiva roster
identity). Alerts group by the roster identity. The queries use the old
`machine_name` label when the same group has no usable sample under
`machine`. The 24-hour and 5-minute silence windows resolve this independently,
so an offline older Collector stays in the known set during rollout.
Thresholds reflect the host capacity and co-tenancy.

The native rollout regression evaluates the shipped queries with the
`promtool` binary from the Prometheus release pinned in `deploy/lgtm/native/versions.yml`.
From the repository root, write the fixture to a temporary path and run it with
that binary:

```bash
fixture="$(mktemp)"
.venv/bin/python -m scripts.data_plane_ops.tests.alert_rules.test_infra_machine_label_rollout \
  > "$fixture"
"$PROMTOOL" test rules "$fixture"
```

| uid | Metric | Condition | `for` | Severity |
|-----|--------|-----------|-------|----------|
| `meiva-ops-host-cpu-saturated` | non-idle CPU | avg by machine > 0.90 (Prometheus) | 15m | warning |
| `meiva-ops-host-memory-pressure` | memory utilization | avg by machine > 0.90 (Prometheus) | 15m | warning |
| `meiva-ops-host-disk-watermark` | filesystem utilization | max by machine+mountpoint > 0.90, excluding read-only/optical-image filesystems and /Volumes/*, /private/tmp/*, /private/var/folders/* (Prometheus) | 15m | warning |
| `meiva-ops-host-disk-watermark-93` | filesystem utilization | same series > 0.93 — approaching the Loki WAL hard stop (Prometheus) | 15m | warning |
| `meiva-ops-host-disk-watermark-95` | filesystem utilization | same series > 0.95 — at the Loki WAL hard stop (Prometheus) | 5m | critical |
| `meiva-ops-host-disk-free-low` | data-volume free bytes | min by machine of `system_filesystem_usage_bytes{state="free"}` on `/` or `/System/Volumes/Data` < 20 GiB (Prometheus) | 5m | warning |
| `meiva-ops-host-disk-free-critical` | data-volume free bytes | same series < 10 GiB (Prometheus) | 2m | critical |
| `meiva-ops-host-disk-draining` | hours until the volume is empty | free bytes ÷ the last hour's loss, only while the loss exceeds 8 GiB, < 6 h (Prometheus) | 10m | warning |
| `meiva-ops-host-swap-used-high` | swap in use | max by machine of `system_paging_usage_bytes{state="used"}` > 8 GiB (Prometheus) | 10m | warning |
| `meiva-ops-host-swap-used-critical` | swap in use | same series > 15 GiB (Prometheus) | 5m | critical |
| `meiva-ops-pgbouncer-write-stall` | PgBouncer per-minute stats | `pgbouncer_query_time_microseconds` > 300000 while `pgbouncer_transactions_per_second` < 20 (Prometheus) | 3m | critical |
| `meiva-ops-pg-connection-saturation` | Postgres backends vs max | ratio > 0.80 (Prometheus) | 15m | warning |
| `meiva-ops-redis-memory` | Redis resident set | > 2 GiB (Prometheus) | 15m | warning |

Collector delivery layer — each sidecar scrapes its per-unit loopback
self-metrics endpoint (`MEIVA_OTELCOL_METRICS_PORT`, default 8888) and relays it
through `metrics/infra`. Prometheus's OTLP translation adds `_total` to the raw
monotonic counters:

| uid | Metric | Condition | `for` | Severity |
|-----|--------|-----------|-------|----------|
| `meiva-ops-otelcol-queue-pressure` | exporter queue size/capacity | current ratio > 0.80 per machine+exporter+signal | 5m | error |
| `meiva-ops-otelcol-enqueue-failures` | new enqueue rejections | `increase(otelcol_exporter_enqueue_failed_*_total[5m]) > 0` | 0m | error |
| `meiva-ops-otelcol-cluster-allow-drops` | records dropped by the collector's cluster filter | `increase(otelcol_processor_filter_{logs,spans,datapoints}_filtered_total{filter="filter/cluster_allow"}[5m]) > 0` (one `increase` per signal: a single `__name__=~` selector collides on labelset) | 0m | error |
| `meiva-ops-otelcol-host-silent` | recently-seen collector absent | machine seen in 24h has no `otelcol_process_uptime_total` in 5m | 0m | error |
| `meiva-ops-otelcol-receiver-refused` | intake refusals | `increase(otelcol_receiver_refused_log_records_total[10m]) > 0` (the memory limiter refuses all three signals at once) | 10m | warning |

The queue rule uses current gauges so it resolves after recovery; the reject
rule uses a bounded counter delta so one historical drop does not keep
alerting until the process restarts — it resolves after a clean 5-minute
window. The silence query's 5-minute absence window is already its
debounce, hence no second `for` delay. Its 24-hour historical machine set expires
retired machines naturally; the fleet heartbeat owns permanent membership.

Prometheus receiver loss — the receiver's own counter records samples older
than its out-of-order intake window:

| uid | Metric | Condition | `for` | Severity |
|-----|--------|-----------|-------|----------|
| `meiva-ops-prom-too-old-samples` | too-old OTLP samples dropped | `increase(prometheus_tsdb_too_old_samples_total[10m]) > 0` | 0m | warning |

The counter covers both whole-batch HTTP 400 loss and silent partial drops;
collector logs alone miss the latter. The window rationale and acceptance
runbook are in the [LGTM README](../../../../README.md#otlp-late-sample-window-out-of-order-intake).

Storage and store growth — absolute-size gauges from the OTLP metric mirror
(`meiva_checkpoint_table_sizes` / `meiva_memory_search_stats`), queried from Prometheus. The exporter's unit
translation renders the dimensionless gauges with a `_ratio` suffix — the
series the rules read are e.g. `meiva_checkpoint_table_sizes_blobs_bytes_ratio`:

| uid | Group | Metric | Condition | `for` | Severity |
|-----|-------|--------|-----------|-------|----------|
| `meiva-ops-checkpoint-blobs-freshness` | `meiva-ops` | checkpoint table size emitter silence | no sample of any of the three series in Prometheus for 2h (absent_over_time) | 5m | warning |
| `meiva-ops-checkpoint-blobs-growth` | `meiva-ops` | checkpoint tables footprint growth | +1 GiB across the three tables in the trailing 6h (Prometheus) | 15m | warning |
| `meiva-ops-memory-search-rows-warning` | `meiva-ops` | memory-search store rows | > 30000 (Prometheus) | 2h | warning |
| `meiva-ops-memory-search-rows-critical` | `meiva-ops` | memory-search store rows | > 100000 (Prometheus) | 2h | critical |

The two checkpoint guards came out of the #4002 evaluation items (#4004/#4005).
Their UIDs keep the historical `blobs` scope they were born with; since
T5280 (2026-10-17) both cover the whole checkpoint footprint — the absolute
2.5/4 GiB tiers were retired because full retention makes an absolute size
tier fire forever, and capacity is guarded by the host disk-watermark rules.
The freshness rule fires on the silent-NoData class itself — the absolute tiers
above go quiet exactly when the series stops, which is how the 24-day miss
stayed invisible — with the 2h window chosen to clear every restart gap
observed in the retained data (one 8.5h outlier fires, correctly). The growth
rule reads the trailing-6h delta of the same gauge (calibrated on the
2026-09-13 burst: quiet ~ MB/6h, peak +10.3 GiB/6h, warn at +1 GiB/6h), so an
escalation is visible hours before the next absolute tier moves.

The five slow-request rules (R17-R19) close the user-visible-latency gap:
R17's fast-route thresholds are calibrated against seven days of route data,
R19 gives slow-by-design routes separate 5s/10s thresholds calibrated against
24 hours of route data, and R18 catches fleet-wide slowdown (p95 vs the 24h
baseline ×2) rather than single long turns. The emitter's single
route-classification source is `gateway/http/middleware/latency.py`. All five carry
`notify_im: "false"` — the PM slow-request convention is warning-first and no
fan-out to the user (alert-fatigue ruling 2026-08-22). The label is history: the
IM channels retired on 2026-11-08 and nothing reads it.

Severity follows the alert-system vocabulary (Task #1224):
critical/warning/error — none of the three is gated. Thresholds are
calibrated against live data (2026-08-04, see header comment in
`rules.yml`).

R13 (llm-billing-quota) is the one rule with no threshold and no `for`
window: an out-of-credit API key fails every turn in the fleet and only a
human spending money clears it, so the first rejection is already the whole
incident. Its discriminator is the `billing` field the emitter writes from
`base/lm/errors.py`'s cross-provider predicate (HTTP 402 plus a per-vendor
vocabulary matched against the response body's `error.type` AND `error.code`) —
a new provider is covered by adding its string there, with no edit to
`rules.yml`.

### Turn p95 by agent

In Grafana Explore with the Loki datasource, run this instant query to count
agents whose own p95 exceeds 75 seconds over the same ten-minute window.
The fleet Turn p95 alert evaluates against Prometheus independently.

```logql
count(quantile_over_time(0.95,
  {service_name="unknown_service", event_name="turn_end", agent_id!=""}
  | json | cluster=".meiva" | category="telemetry"
  | unwrap attributes_duration_seconds | __error__="" [10m]) by (agent_id) > 75) or vector(0)
```

### Migration notes (Postgres → LGTM, 2026-08-12)

- **R1/R2/R5** — same semantics, Loki `count_over_time` /
  `sum_over_time` + `| json` filters. R2 unwraps `attributes_n` (SUM of the
  real per-row drop counts, the same contract the Ops panel reads); R5's
  freshness discriminator is the numeric comparison
  `| attributes_age_s < 600` on the flattened label.
- **R4** — p95 now comes from `histogram_quantile(0.95,
  sum by (le) (rate(meiva_llm_usage_latency_milliseconds_bucket[10m])))`,
  an approximation of the exact SQL percentile (the same read the Insights
  Ops panel uses). Absent samples → NoData → OK (no spurious night fires).
- **R6** — semantics changed with the stream: the old rule measured the
  retired events TABLE's write freshness; the new rule probes the live
  stream with `absent_over_time({service_name="unknown_service"}[5m])` —
  fires when no event of ANY kind reached Loki in 5m (emitter stall or a
  broken OTLP export path alike — the 2026-08-12 preflight-bug outage
  class), at the cost of no longer distinguishing the two. When the stream
  flows the query returns no data (NoData → OK).
- **R4/R6 data-source caveat** — every rule reads Loki/Prometheus now; if
  the OTLP export path fails (as it did 2026-08-12), new events stop
  landing and R1-R5, R13 go quiet with them — R6 exists exactly to scream
  about that outage class, and the health-probe chain covers the rest.

## Sync to the live Grafana

Converge renders the provisioning tree into this home's native configuration.
Alert-rule changes require a new root generation. Use a full `meiva stop` (every choice explicit,
e.g. `meiva stop --yes --close-shell-sessions --stop-browser --stop-extras --keep-no-services --stop-infra --mode smooth --timeout 300`) then
`meiva start --all-services`; readiness alone cannot prove that a running Grafana reloaded a file.
There is no independent launchd or systemd Grafana restart path.

## How to add a rule

1. Add a rule to `rules.yml` (uid must be unique; keep `folder: Meiva`). Put fast
   rules in `meiva-ops` (`interval: 1m`) and expensive slow-request rules in
   `meiva-ops-slow` (`interval: 5m`). Loki queries: stream selector
   `{service_name="unknown_service"}` + `| json` before any field filter,
   and wrap every count in `sum(...)` — the unknown_service family has >500
   streams over a day and an unaggregated count hits Loki's per-query
   series cap.
2. Land the change; on the LGTM host restart native Grafana
   (rule provisioning does not hot-reload).
3. Verify via native Grafana's admin API:
   `GET /api/v1/provisioning/alert-rules`. Synthetic events are no longer SQL
   inserts — push a test event through the emitter (any Meiva process emits
   on activity; e.g. trigger a real event or use the Loki push API), then
   watch `GET /api/prometheus/grafana/api/v1/rules` for the state
   transition.

## Signal rules — the alerts that used to be written in code

Before 2026-10-04 several processes wrote their own alerts (a direct row plus a direct IM)
and several gates throttled or graded them (consecutive rounds, grace windows, cooldown
markers, the 180s/600s transition clock, deploy-window holds). All of that is gone
([decision](../../../../../../docs/decisions/observability/alerts/2026-10-04-alerting-on-grafana-alerting.md)): the code
emits a declared event (`category=telemetry`) while a condition holds, and the rule's `for:`
and trailing window carry the debounce. A condition that stops resolves itself when its events stop.

| uid | Event | `for` | Severity | Replaces |
|-----|-------|-------|----------|----------|
| `meiva-ops-health-probe-warning` / `-error` | `health_probe_failing` per `attributes_check` | 3m / 10m | warning / error | the probe's time-graded owner alert |
| `meiva-ops-health-probe-silent` | absent `health_probe_ran` for 15m | 0m | error | nothing: a stopped probe was silent  |
| `meiva-ops-service-start-unready` | `service_start_unready` | 0m | warning | start-readiness alert rows |
| `meiva-ops-boot-retries-exhausted` | `boot_retries_exhausted` | 0m | error | nothing: the boot job's endless retry never reported a permanent failure |
| `meiva-ops-schedule-verify-failed` | `schedule_verify_failed` | 0m | error | the verify sweep's direct post |
| `meiva-ops-schedule-stalled` | `schedule_stalled` (24h) | 0m | warning | the event had no rule |
| `meiva-ops-schedule-convergence-abandoned` | `schedule_convergence_abandoned` (24h) | 0m | warning | nothing: the manager's reap and sync retries had no bound and no report |
| `meiva-ops-machine-offline-warning` / `-error` | `machine_probe_failed` per machine | 3m / 10m | warning / error | the machine-offline rows and their clock |
| `meiva-ops-machine-stopped-too-long` | `machine_stopped_too_long` per machine (an announced stop, 20m+ old, no `--maintenance-for` window) | 0m | error | nothing: an intentionally stopped host leaves the probe target set, so a forgotten stop was invisible inside the cluster (10-14: 53 minutes) |
| `meiva-ops-agent-host-down` | `machine_agent_host_down` per machine (the ops server answers the probe, the agent host does not run; 2m window, so three consecutive passes) | 3m | error | nothing: the probe succeeded, so no offline rule and no `machine_probe_failed` saw it |
| `meiva-ops-exec-child-boot-failed` | `exec_child_boot_failed` | 0s | warning | the exec child's direct post and 600s limit |
| `meiva-ops-delivery-stalled` | `delivery_stalled` per agent | 1m | warning | the deploy-window hold and settle grace |
| `meiva-ops-delivery-stranded-wake` | `delivery_stranded_wake` per agent (inbound id in the summary) | 0s | warning | nothing: an automatic wake that revives an owner a user had just killed had no notification path (2026-10-25 review of #7066) |
| `meiva-ops-db-pool-acquire-slow` | `db_pool_acquire_slow` per machine (the agent host's shared pools; a borrow carries no agent) | 2m | warning | the per-host cooldown marker |
| `meiva-ops-root-unit-restart-failed` / `-absent` / `-breaker-open` | `root_unit_failure_state` per `attributes_kind`, per machine and unit | 0m | error / error / critical | the root unit episode store and its posts; `-absent` adds the state nothing recorded a cause for (task #6345) |
| `meiva-ops-root-unit-not-revivable` | `root_unit_not_revivable` >1 in 2m | 0m | error | `terminal_escalate_rounds` |
| `meiva-ops-root-unit-stop-escalated` | `root_unit_stop_escalated` >0 in 10m | 0m | error | nothing: a stop that outlived its window gave up silently and left a half-dead unit (2026-10-18) |
| `meiva-ops-root-diagnostic-venv` | `root_diagnostic` verdict `down` / `port-taken`, >0 samples in 3m (a measured finding alerts on the first sample) | 0m | warning | the per-probe consecutive-failure thresholds |
| `meiva-ops-root-diagnostic-no-verdict` | `root_diagnostic` verdict `inconclusive` / `unavailable` per machine and diagnostic, >2 samples in 3m (three rounds at the 60s cadence) | 0m | warning | nothing: a host busy enough to starve a read-only check reported itself as a broken venv or brew pin set instead (2026-10-27, #5656). No-verdict samples are excluded from the failure rules above, so the run needs its own rule |
| `meiva-ops-root-diagnostic-browser-reach` | `root_diagnostic` verdict `down` / `unavailable` / `port-taken`, >2 samples in 16m | 0m | warning | the per-probe consecutive-failure thresholds; its cadence (300s) is why one debounce covers both classes over ~10-15 minutes |
| `meiva-ops-schedule-not-progressing` | `schedule_not_progressing` (10m) | 0m | warning | nothing: the schedule's own next-round deadline is the FIRST level of a stall, and only `schedule_stalled` (two hours later) ever had a rule (#6400) |
| `meiva-ops-llm-turn-aborted` | `llm_turn_aborted` per agent (30m), excluding `cause=planned_restart` (an operator's stop or restart cut the turn) | 0m | warning | nothing: `llm_provider_error` matches only 429 and billing rejections, so every permanent 400/401/403/404/422 ended a turn unheard (#6400) |
| `meiva-ops-llm-usage-metering-failed` | `llm_usage_metering_failed` per model (1h) | 0m | warning | nothing: the call ran and its `llm_usage` row was dropped, so the spend happened and the ledger never saw it (#6400) |
| `meiva-ops-llm-usage-unpriced` | `llm_usage_unpriced` per model and cause (15m) | 0m | error | the per-call `[llm usage] ... is unpriced` warning, which only a log reader saw; the same alert covers a model the catalog does not know and a cache-write class it has no rate for |
| `meiva-ops-exec-node-timeout` | `exec_node_timeout` per agent (30m) | 0m | warning | nothing: the node timeout killed the call and no rule read the event (#6400) |
| `meiva-ops-exec-subprocess-killed` | `exec_subprocess_killed` per agent (1h) | 0m | warning | nothing: a child that outlived the signal grace may have left partial output behind (#6400) |
| `meiva-ops-alert-pipeline-watchdog` | Prometheus `vector(1)` — always firing | 0s | warning | synthetic heartbeat: it exists only so R28 has something whose absence means something. Its route repeats every 5m and does not fall through, so it costs one webhook POST per period |
| `meiva-ops-alert-pipeline-silent` | `time() - meiva_alerts_last_ingest_timestamp_seconds` > 900 (three watchdog periods) | 5m | error | nothing: a rotated webhook token or a misrouted contact point silences **every** rule at once, and no rule rode a path that could report it (2026-11-04) |
| `meiva-ops-grafana-rule-evaluation-failed` | At least one terminal Grafana rule evaluation failure in 5m, aggregated across rules | 0m | error | rule evaluation health in Grafana and its alerting logs |

Unhealthy-for-3-minutes and unhealthy-for-10-minutes are two coexisting rules per condition
(Grafana's file provisioning has no inhibition rules), so a long outage holds a warning and an
error instance; the machine-offline pair hands off through the `consecutive_failures` filter instead.
The observatory station's own reachability has no rule: Loki and Grafana live on the station and
the events travel the very path it would watch. An ingress that rejects events and a station that is
down both surface as the health probe's `health_probe_ran` going absent; a whole-machine outage is the
out-of-band probe's.

## Rule evaluation failures

Prometheus scrapes Grafana's `grafana_alerting_rule_evaluation_failures_total`
counter. The meta-alert reports increments within five minutes as one error
instance, so a datasource outage affecting many rules produces one alert. With
the station's agent inbox policy installed, this error reaches the on-call agent.
Grafana gives a query failure a DatasourceError state for this one meta-alert.
The external sentinel has an independent agent-message path when it cannot
query Prometheus or when the watchdog heartbeat disappears.

## The alert pipeline's own dead-man pair

Grafana delivers the watchdog to the gateway at `POST /api/alerts`. A webhook token
rotation, stopped gateway, or wrong contact point can break that path for every
rule. The watchdog sends a heartbeat to make failures visible on a quiet night.

- `meiva-ops-alert-pipeline-watchdog` fires unconditionally (`vector(1)`) every evaluation. It asserts
  nothing about the system; its only job is to walk the whole path and refresh one gauge. Its route
  repeats every 5 minutes (`repeat_interval: 5m` checked on a `group_interval: 1m` tick, so the
  measured period is five to six minutes; with the two intervals equal Alertmanager skips every other
  beat) and carries `continue: false`, so the heartbeat costs one POST per period
  and never reaches the catch-all.
- `meiva-ops-alert-pipeline-silent` reads the gauge the gateway refreshes
  (`alerts_last_ingest_timestamp_seconds`, emitted by `gateway/alerts/router.py:receive_alert_webhook`
  for every accepted POST that carries a firing watchdog) and fires when it is older than 900 s — three
  missed periods — for 5 minutes. The outside-Grafana sentinel uses the existing agent message path and judges the
  age of the newest heartbeat over the last hour (`max_over_time(...[1h])`), so a gateway restart that
  replaces the series is not silence; it fires when that age passes 1800 s.

The judgment is on the *age of the last accepted heartbeat*, not on how many alerts fired: a quiet night
cannot trip it and a broken token cannot hide behind one. The period (5 m) and the threshold (3 × 5 m)
are asserted together in `tests/scripts/test_alert_notification_policy.py`, so one cannot drift from the
other.

The host sentinel runs every 15 minutes (task #6220) and can observe a stopped
Grafana engine. When the gateway stops, the ingest gauge can disappear with it.
Grafana's silent-pipeline rule leaves an empty query in the OK state, while the
sentinel checks for the loss of a previously observed series. The separate
`meiva-ops-gateway-metrics-silent` rule tracks gateway metric freshness.

## Notifications — the policy, the webhook, the window silence

Rules evaluate in Grafana, and Grafana's embedded Alertmanager owns the alert state: what is
firing, silenced or recovering. The policy (`contact.yml`) is the one place merging and repetition
are decided: instances of one rule are one group (`group_by: [alertname]`, 30s wait, 5m
interval, 4h repeat). The built-in policy has one receiver, the gateway's `POST /api/alerts`,
which stores nothing — it only counts the watchdog heartbeat (its route repeats every 5m). The
direct Telegram contact point is retired. The host sentinel retains a separate observation loop and
agent-queue submission, but no external Bot API delivery path remains.

The gateway keeps no copy of alert state. The Web UI's alert badge and `meiva stats` read Grafana's
active alerts live (`GET /api/alerts/firing`; silenced and inhibited instances do not count, and a
Grafana that cannot be reached shows as unavailable, never as zero). History is Grafana's: the
alerting history page, `GET /api/annotations?type=alert` and `GET /api/v1/rules/history`.
The retired IM channel's `notify_im` labels are inert.

For an agent inbox destination, start with the [complete site policy example](../../contact.agent.example.yml).
Copy it to `$MEIVA_HOME/lgtm/alerting/contact.yml`, replace the example agent ID, and converge the
station, then reload Grafana alerting provisioning. Its error/critical route continues to the
heartbeat route. The built-in policy notifies nobody. [Receiver setup](../../../../../../gateway/alerts/docs/alerts.md#alertmanager-agent-receiver)

A planned outage is a silence, not a gate in the services: `meiva update down` creates
one silence in this Grafana (matcher `alertname=~".+"`, disk alerts excepted: `metric="host_disk"`,
`attributes_check="disk_usage"`; createdBy `meiva-fleet-update`; expiry and a comment naming the
target) and `up` expires it. Silenced rules keep evaluating, so a condition that outlives the window
notifies when the silence ends.

## Events-stream cutover (task #1197, done)

The unified emitter ships events as OTLP logs to Loki. Explicitly mapped
telemetry fields feed Prometheus. Loki and Prometheus are the only stores of an event; nothing keeps a local copy.
These rules read LGTM.

## Metric-name test evidence

`tests/scripts/test_alert_rules.py` checks the PromQL references in both the
rules and the Ops dashboard against the instrument names and kinds in the
[explicit metrics mapping](../../../../../../base/telemetry/otlp/docs/telemetry-otlp/metrics-mapping.md).

### Platform watchers these rules replace (R9)

Before this change the second alert plane ran its own platform checks outside Grafana:
schedules and launchd jobs that sampled a machine over ssh or tailed a log, kept their own
state file and cooldown, and sent a message straight into an agent's inbox. A check that
belongs to the platform is a signal plus a rule here, routed by `severity` through
Alertmanager. Replaced, with what is *not* covered:

| Watcher group | Replaced by | Not covered |
|---|---|---|
| macmini / company-mini free-space and rapid-drop watchers (`mm-descend-watch`, `disk-rapid-watch`, `disk-monitor`, `cm-watch`, `cm-avail-watch2`, `tier1-trigger`, the launchd disk samplers' alert half) | `host-disk-free-low`, `host-disk-free-critical`, `host-disk-draining` | per-process write attribution and the `disk17` emergency reclaim (an action, not an alert; it keeps its own trigger) |
| swap watchers (`swap-guard-v15`, `cm-pool-watch`, `cm-mem-watch`) | `host-swap-used-high`, `host-swap-used-critical` (swap in use, from the agent host's own gauge because the host_metrics `paging` scraper reads 0 on macOS) | kernel vm-pressure level and jetsam kills (not swap-pool signals); the page-out rate and the swap-pool growth steps have no signal here |
| `db-write-stall-watch` (#39) | `pgbouncer-write-stall` | none: same 3-minute judgement over the same stats line |
| `cm-4318-probe` (#149) | `otelcol-receiver-refused`, `otelcol-host-silent` | the :4318 empty-POST reachability probe |
| host-liveness watchers (`company-air-probe`, `mba-probe`, `monsora-sre-host-liveness-crosscheck` and its watchdogs) | `machine-offline-*`, `machine-stopped-too-long`, `health-probe-*` | an agent host that is dead while its machine's ops endpoint still answers (the heartbeat records `agent_host_online`; no rule reads it) |
