# OTLP metrics mapping

`METRIC_DISPOSITION` in `base/telemetry/otlp/plans.py` lists each event field
that produces an application metric. A new event or payload field requires an
explicit entry and a Grafana consumer to produce a metric. The event stream
keeps the full payload, including LLM token and cost observations, precomputed
gateway latency percentiles, and calendar dates.

Each entry sets the instrument kind independently of whether the numeric
sample arrived as an integer or float. The instrument name is
`meiva_<event_name>_<field-minus-unit-suffix>`. The OTel unit is `ms` for
`_ms`, `s` for `_seconds`, and `1` for dimensionless fields. Prometheus
translates units and instrument kinds into suffixes, for example
`meiva_llm_usage_latency_milliseconds_bucket` and `meiva_gateway_latency_count_total`.
The LLM latency histogram uses explicit boundaries from 250 ms to 300 s.

## Series identity

The source collector stamps the roster `machine` label. Application
datapoints carry only dimensions consumed by the rules:

- The queue-loss timestamp gauge keeps the process and the known queue names
  `emitter`, `otlp`, and `otel-sdk`. Delayed loss summaries cannot rewind it.
- Recovery status keeps the declared `logical_dump` drill.
- Root expectation and tick gauges keep the process and a fixed `home_id`
  hash for each configured root unit home. The freshness rule joins expected
  and observed rounds on `machine`, `home_id`, and `process`. Keeping the
  home dimension prevents a healthy root from satisfying another same-host
  root's expectation, including when two homes share a basename.

Other application datapoints use only their resource identity and the
collector's machine label. The resource carries `service.name=meiva-<process>`,
`service.namespace=meiva`, `service.version`, the cluster label, and one
`service.instance.id` per process. The instance keeps concurrent cumulative
streams distinct. The four short-lived roles `agent-exec`, `sdk`,
`schedule-runner`, and `cli-health-probe` export their events as logs and
retain the independent queue-loss timestamp gauge. Their metric views
drop all other instruments, including SDK diagnostics. In the absence
of queue loss, these roles create no Prometheus series.

The periodic metric reader sends OTLP to the local sidecar every 15 seconds.
The PR flow table and the standalone per-agent turn latency diagnostic query
Loki for day and agent dimensions. Fleet turn p95 and the gateway heartbeat
use Prometheus.
