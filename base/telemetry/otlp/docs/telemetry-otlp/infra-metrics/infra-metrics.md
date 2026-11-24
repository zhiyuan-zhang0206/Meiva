# Infrastructure metrics — the sidecar's own scrapes

Every other metric in this subtree is *pushed* by an Meiva process. This one is
**pulled by the collector**: `deploy/otel-collector/otel-collector.yaml`
declares receivers that scrape the machine and its data plane directly, so no
Meiva code participates and a box with no agents running still reports.

## What is scraped

- `host_metrics`, on every **collector-bearing** machine, every 30 s — cpu (utilization ratio
  only; the per-core cumulative `system.cpu.time` is disabled), load, memory,
  disk, filesystem, network.
- `prometheus/otelcol`, on every **collector-bearing** machine, every 30 s from the collector's
  loopback self-metrics endpoint — exporter queue size/capacity, enqueue
  failure counters and process uptime. Its port is per-unit configurable via
  `MEIVA_OTELCOL_METRICS_PORT` (default 8888), so two units on one host do not
  collide. These are the delivery path's own observability: queue pressure and
  new drops must not hide behind a receiver that merely remains alive.
- `postgresql` + `redis`, every 60 s, on a **gateway-capable unit only** —
  this cluster's own data plane. Postgres is dialed DIRECT (never PgBouncer:
  `pg_stat_*` over a transaction-pooled session is not trustworthy) over the
  home's owner-only unix socket as the stable monitoring role `meiva_monitor`,
  admitted by `peer` (`base/cluster/authority/monitor.py`): no password
  exists, the config's `password` is a fixed non-secret placeholder the contrib
  receiver's validation demands, and a write-generation rollout does not touch
  it. The role reads statistics (`pg_read_all_stats`) and no application row.
  A remote-managed plane has no such socket or role here and omits the Postgres
  receiver. Redis authenticates with the gateway-local Redis-admin password,
  which is also its `requirepass`. A pure agent-runner's URLs point at the
  GATEWAY's data plane, so rendering those receivers there would only duplicate
  the gateway's series — `_data_plane_fragments` in
  `cli/commands/observability/otel_collector.py` omits them, and the rendered config is 0600
  because it carries the Redis admin password (and, on split units, the
  telemetry token).

- `filelog/pgbouncer` → `signal_to_metrics/pgbouncer`, on the same gateway-capable,
  home-owned-plane units as the Postgres receiver: the pooler's per-minute
  `LOG stats:` line becomes the gauges `pgbouncer_transactions_per_second` and
  `pgbouncer_query_time_microseconds` (latest line wins). It reads the log, not
  the admin console, so no credential is involved; the lines go through their own
  logs pipeline and never reach Loki. The write-stall rule on these gauges lives in
  Grafana (it replaced the `db-write-stall-watch` schedule).

Not a node_exporter / postgres_exporter / redis_exporter trio: the pinned
contrib collector already carries equivalent receivers, and one supervised
binary per machine is the whole point of the sidecar architecture.

## Labels

The collector stamps locally collected signals with the `meiva.machine`
resource attribute from the unit's roster identity at converge. For both
`metrics` and `metrics/infra`, `transform/metric_machine` copies it to the
`machine` datapoint attribute so Prometheus queries and alert rules can use it
directly. Authenticated relays preserve the sender's identity.

The infra receivers use their own pipeline for extra dimensions:

- `resource_detection/host` supplies the physical OS hostname as `host.name`.
- `resource/infra` sets `service.name=meiva-infra`, which Prometheus maps to
  `job="meiva-infra"`.
- `transform/host_label` copies `host.name` to datapoints. Prometheus exposes it
  as `host_name`, alongside `machine`.

Cardinality is filtered at the source, not in Prometheus: synthetic mounts
(`/System/Volumes/*`, devfs, autofs …) and macOS's ~30 virtual interfaces
(awdl/llw/utun/…) are excluded, which took one machine's infra series from
~350 to ~180.

## One history

Prometheus is the only store that retains these. `meiva status` and the status
page carry a single LIVE reading per machine (`base/host/resource/sample.py`) so
they still answer on a deployment whose LGTM backend is down or was never
deployed; the retired `base/resource_monitor.py` kept a parallel 300-sample
ring buffer, which meant two drifting answers to "what was the CPU on machine
X" and lost its history on every process restart.

## Reading a past window

Live, past and per-process readings, and the one metric that survives a starved
host: [reading a past window](reading-a-past-window.md).

## Not gated by the OTLP kill switch

`MEIVA_TELEMETRY_OTLP_ENABLED` is producer-scoped (the event dual-write, trace
recording, ship) and does not gate a sidecar that its role/home allows. These
scrapes are the collector's own, so a marked gateway or pure runner reports
host health even with the event stream reduced to its JSONL mirror only. An
unmarked gateway has no collector. Silencing an allowed collector means
stopping the sidecar
(`meiva start --disable-service otel-collector`) or the backend (`meiva observability off`);
with no backend reachable the Prometheus exporter's bounded retry drops them,
the same shed path app metrics already take. On a pure runner the exporter
targets the gateway collector's authenticated private receiver; only the
gateway collector writes to loopback Prometheus.

## Gaps

- **PgBouncer** has no OTel contrib receiver (`SHOW STATS` is not the Postgres
  wire protocol), so pool saturation is watched at Postgres — backends against
  `max_connections`.
- **Per-process attribution** is absent from the scrapes: the `host_metrics`
  process scraper is unsupported on macOS (what prod runs) and filters by process
  NAME, which cannot separate an Meiva agent from any other `python3.12` on the
  box. A Linux host covers it with the timer-driven sampler described above; macOS
  hosts still have no per-process record.

## Notes

- Alert rules R8-R12 read host/data-plane series. R14-R16 read collector
  delivery series: current queue ratio over 0.80, `increase` of the translated
  `otelcol_exporter_enqueue_failed_*_total` counters over 5 minutes, and a
  machine seen in 24h whose `otelcol_process_uptime_total` disappeared for 5
  minutes.
  The suffixes are Prometheus 3's OTLP translation, not the raw collector
  self-metrics names.
  Rules and the `meiva-ops-main` dashboard (its "Host & data plane" section) live in
  `deploy/lgtm/config/grafana/provisioning/`.
- Metric names were read off a live Prometheus 3.13.2, not inferred from the
  OTLP names: `system_cpu_utilization_ratio`,
  `system_filesystem_utilization_ratio`, `postgresql_backends`,
  `postgresql_connection_max`, `redis_memory_used_bytes`, …
- The operator's reading loop is `docs/conventions/runbook.md` ("The operator's SRE
  loop"); the thresholds are deployment config, never framework constants.
- Parent node: [OTLP export backend](../telemetry-otlp.md).
